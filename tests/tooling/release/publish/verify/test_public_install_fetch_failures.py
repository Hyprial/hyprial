"""verify_public_install: "could not read" is not "does not exist".

2026-10-30 v0.5.5.2.2: one TLS EOF on ``git/ref/tags/internal`` returned
HTTP -1 and was reported as "the tag does not exist on the mirror", failing
the release before its track step. Transport failures are retried; a
persistent one is reported as unverifiable, while a real 404 stays "absent".
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "scripts" / "verify" / "verify_public_install.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_public_install", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


README = "uv tool install git+https://github.com/Hyprial/hyprial.git@internal\n"


def _fake(module, tag_responses: list[int]):
    calls: list[str] = []

    def get(url: str):
        calls.append(url)
        if url.endswith(f"/git/ref/tags/{module.CONTROL_TAG}"):
            return 404, b""
        if "/contents/README.md" in url:
            body = {"content": base64.b64encode(README.encode()).decode()}
            return 200, json.dumps(body).encode()
        status = tag_responses.pop(0) if tag_responses else -1
        body = json.dumps({"object": {"sha": "a" * 40}}).encode() if status == 200 else b""
        return status, body

    return get, calls


def _run(monkeypatch, capsys, tag_responses):
    module = _load()
    get, calls = _fake(module, list(tag_responses))
    monkeypatch.setattr(module, "_get", get)
    monkeypatch.setattr(module, "_sleep", lambda _s: None, raising=False)
    monkeypatch.setattr("sys.argv", ["verify", "--repo", "Hyprial/hyprial", "--commit", "b" * 40])
    code = module.main()
    return code, capsys.readouterr().out, calls


def test_transient_fetch_failure_is_retried_and_passes(monkeypatch, capsys) -> None:
    code, out, calls = _run(monkeypatch, capsys, [-1, 200])
    assert code == 0, out
    assert "✅ tag internal: 200" in out
    assert sum(u.endswith("/git/ref/tags/internal") for u in calls) == 2


def test_persistent_fetch_failure_is_unverifiable_not_absent(monkeypatch, capsys) -> None:
    code, out, _calls = _run(monkeypatch, capsys, [-1, -1, -1, -1])
    assert code != 0
    assert "unverifiable(fetch_failed)" in out
    assert "不存在" not in out.split("control:")[-1].split("\n", 1)[-1]


def test_real_404_is_still_reported_absent(monkeypatch, capsys) -> None:
    code, out, _calls = _run(monkeypatch, capsys, [404])
    assert code != 0
    assert "tag internal: HTTP 404" in out
    assert "不存在" in out
    assert "unverifiable" not in out
