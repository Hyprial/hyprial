"""§8.2: a 429/quota provider failure must fail fast, on exactly one attempt.

Production requirement (docs/notes/resident-pi-provider-credentials-2026-10-16.md
§8.2): the same request must not become three (or three-times-pi-retries)
provider calls, and the waiting sender must get one availability notice, not
one per attempt.

This file pins the Pi side of the budget: the real controlled loader drives the
repository's fake provider, which always answers 429, and the provider counts
the requests that actually arrive.  A single completion must produce a single
call -- hyprial's durable inbox owns retry, so pi's two retry layers are capped
at zero for this launch.  The daemon-side classification that stops hyprial
from redelivering the turn is pinned next to the turn runtime.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.pi.loader import PI_CONTROLLED_LOADER

ROOT = Path(__file__).resolve().parents[5]
NODE = shutil.which("node")
PI = shutil.which("pi")

FIXTURE = ROOT / "tests/fixtures/agent_home_p20/pi"


def _load_probe(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_probe_{name}", ROOT / "contract" / "e2e" / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pi_package_root() -> Path | None:
    if PI is None:
        return None
    candidate = Path(PI).resolve()
    for parent in (candidate, *candidate.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            body = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if body.get("name") == "@earendil-works/pi-coding-agent":
            return parent
    return None


PI_ROOT = _pi_package_root()

pytestmark = pytest.mark.skipif(
    NODE is None or PI_ROOT is None,
    reason="the real node and installed Pi SDK are required",
)


def _model_definition(model_id: str, base_url: str) -> dict[str, object]:
    return {
        "api": "openai-completions",
        "baseUrl": base_url,
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "reasoning": False,
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 100_000,
                "maxTokens": 1_000,
            }
        ],
    }


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    repository = fixture / "workspace" / "repo"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    cwd = (repository / "nested").resolve()
    (cwd / ".pi").mkdir(exist_ok=True)
    shutil.copy2(repository / ".pi" / "settings.json", cwd / ".pi" / "settings.json")
    source = fixture / "native-root"
    projection_root = fixture / "projection-root"
    shutil.copytree(source, projection_root)
    (projection_root / "auth.json").unlink()
    native_root = fixture / "native-auth-root"
    native_root.mkdir()
    (native_root / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": "sk-fake-deepseek"}})
        + "\n",
        encoding="utf-8",
    )
    (projection_root / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": "deepseek",
                "defaultModel": "deepseek-flash",
                "defaultProjectTrust": "ask",
                "enableInstallTelemetry": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    home = (tmp_path / "home").resolve()
    home.mkdir()
    session_root = (tmp_path / "sessions").resolve()
    session_root.mkdir()
    return cwd, repository, projection_root, native_root


def test_one_rate_limited_completion_makes_exactly_one_provider_call(
    tmp_path: Path,
) -> None:
    """A 429 must reach the provider once, not four times.

    Pi's own agent retry (``retry.maxRetries``) and provider retry
    (``retry.provider.maxRetries``) both default on; left alone, one prompt
    produces four real calls (measured: 4, 22.8s) before hyprial's durable
    retry even starts.  This test observes the fake provider's request count.
    """

    fake_provider = _load_probe("_fake_provider")
    cwd, repository, projection_root, native_root = _workspace(tmp_path)
    home = (tmp_path / "home").resolve()
    session_root = (tmp_path / "sessions").resolve()

    with fake_provider.FakeProvider(
        reply="ok",
        plan=fake_provider.FaultPlan.fail_always(fake_provider.RATE_LIMITED),
    ) as provider:
        payload = {
            "piPackageRoot": str(PI_ROOT),
            "cwd": str(cwd),
            "repositoryRoot": str(repository.resolve()),
            "projectionRoot": str(projection_root.resolve()),
            "nativeRoot": str(native_root.resolve()),
            "homeRoot": str(home),
            "projectTrusted": True,
            "sessionRoot": str(session_root),
            "sessionId": "rate-limit-session",
            "receiptPath": str((session_root / "receipts" / "rpc.json").resolve()),
            "modelProvider": "deepseek",
            "model": "deepseek-flash",
            "modelDefinitions": {
                "deepseek": _model_definition(
                    "deepseek-flash", provider.openai_base_url
                )
            },
        }
        process = subprocess.Popen(
            [
                NODE or "node",
                str(PI_CONTROLLED_LOADER),
                "run-rpc",
                json.dumps(payload, separators=(",", ":")),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env={
                "HOME": str(home),
                "PATH": os.environ.get("PATH", ""),
                "PI_CODING_AGENT_DIR": str(native_root.resolve()),
                "PI_OFFLINE": "1",
                "PI_TELEMETRY": "0",
            },
            text=True,
        )
        stdout_lines: list[str] = []

        def _read() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stdout_lines.append(line)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        assert process.stdin is not None
        process.stdin.write('{"id":"p","type":"prompt","message":"hello"}\n')
        process.stdin.flush()
        started = time.monotonic()
        stable_since: float | None = None
        last = 0
        while time.monotonic() - started < 120:
            time.sleep(0.5)
            count = len(provider.requests)
            if count != last:
                last = count
                stable_since = time.monotonic()
            elif stable_since is not None and time.monotonic() - stable_since > 8:
                break
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=5)
        # PIPE descriptors outlive the process unless closed explicitly; the
        # suite's fd-hygiene fixture counts them.
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    completions = [path for path in provider.requests if "chat/completions" in path]
    assert len(completions) == 1, (
        f"a single 429 prompt produced {len(completions)} provider calls: "
        f"{provider.requests}"
    )
    errors = [
        json.loads(line)["message"]["errorMessage"]
        for line in stdout_lines
        if line.strip()
        and '"errorMessage"' in line
        and json.loads(line).get("message", {}).get("errorMessage")
    ]
    assert errors and "429" in errors[0]
