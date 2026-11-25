"""``hyprial agent tiers probe`` end to end against a home without users.json."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

OWNER = "alicesmith"


def write_executable(directory: Path, name: str, script: str) -> None:
    path = directory / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_probe(
    home: Path, path_env: str, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_OWNER": OWNER,
        "PATH": path_env,
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "hyprial.cli",
            "agent",
            "tiers",
            "probe",
            *arguments,
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[4],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def cli_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    parsed = json.loads(result.stdout)
    assert isinstance(parsed, dict)
    return parsed


def prepare_cli_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "hyprial-home"
    state = home / "state"
    state.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return home, state, bin_dir


def stored_capabilities(state: Path) -> list[dict[str, Any]]:
    document = json.loads((state / "runtime-capabilities.json").read_text(encoding="utf-8"))
    (owner,) = document["owners"]
    assert owner["ownerKey"] == OWNER
    return owner["capabilities"]


def test_cli_probe_writes_result_and_is_reprobed_without_combo(
    tmp_path: Path,
) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    write_executable(bin_dir, "claude", "#!/bin/sh\necho hello\n")

    result = run_probe(home, str(bin_dir), "--combo", "claude::sonnet")

    assert result.returncode == 0, result.stderr
    payload = cli_payload(result)
    assert payload["ok"] is True
    assert payload["dryRun"] is False
    assert payload["probe"] == "user-runtime-capability"
    assert "profile" not in payload
    assert not (state / "users.json").exists()
    assert payload["changed"] == ["runtimeCapabilities"]
    (entry,) = payload["results"]
    assert entry["harness"] == "claude"
    assert "provider" not in entry
    assert entry["model"] == "sonnet"
    assert entry["status"] == "available"
    assert entry["probedAt"]

    (persisted,) = stored_capabilities(state)
    assert persisted["status"] == "available"

    again = run_probe(home, str(bin_dir))
    assert again.returncode == 0, again.stderr
    assert [entry["model"] for entry in cli_payload(again)["results"]] == ["sonnet"]


def test_cli_probe_dry_run_does_not_write(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    write_executable(bin_dir, "claude", "#!/bin/sh\necho hello\n")

    result = run_probe(
        home, str(bin_dir), "--combo", "claude::sonnet", "--dry-run"
    )

    assert result.returncode == 0, result.stderr
    payload = cli_payload(result)
    assert payload["dryRun"] is True
    assert payload["results"][0]["status"] == "available"
    assert not (state / "runtime-capabilities.json").exists()


def test_cli_probe_records_usage_limit_failure(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    write_executable(
        bin_dir,
        "pi",
        "#!/bin/sh\necho \"You're out of extra usage\" >&2\nexit 1\n",
    )

    result = run_probe(home, str(bin_dir), "--combo", "pi:kimi-coding:k3")

    assert result.returncode == 0, result.stderr
    (entry,) = cli_payload(result)["results"]
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "usage-limit"
    assert entry["provider"] == "kimi-coding"
    assert stored_capabilities(state)[0]["reason"] == "usage-limit"


def test_cli_probe_records_command_not_found(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)

    result = run_probe(home, str(bin_dir), "--combo", "pi:kimi-coding:k3")

    assert result.returncode == 0, result.stderr
    (entry,) = cli_payload(result)["results"]
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "command-not-found"


def test_cli_probe_records_timeout(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    write_executable(bin_dir, "codex", "#!/bin/sh\nexec /bin/sleep 30\n")

    result = run_probe(
        home, str(bin_dir), "--combo", "codex::gpt-5.3-codex", "--timeout", "1"
    )

    assert result.returncode == 0, result.stderr
    (entry,) = cli_payload(result)["results"]
    assert entry["status"] == "unavailable"
    assert entry["reason"] == "probe-timeout"


def test_cli_probe_without_combos_or_declarations_fails(tmp_path: Path) -> None:
    home, _, bin_dir = prepare_cli_home(tmp_path)

    result = run_probe(home, str(bin_dir))

    assert result.returncode == 1
    payload = cli_payload(result)
    assert payload["ok"] is False
    assert "combo" in payload["error"]


def test_cli_probe_rejects_malformed_combo(tmp_path: Path) -> None:
    home, _, bin_dir = prepare_cli_home(tmp_path)

    result = run_probe(home, str(bin_dir), "--combo", "claude:sonnet")

    assert result.returncode == 1
    assert "harness:provider:model" in cli_payload(result)["error"]


def test_cli_probe_surfaces_a_corrupt_cache_as_invalid_argument(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    (state / "runtime-capabilities.json").write_text("{broken", encoding="utf-8")

    result = run_probe(home, str(bin_dir), "--combo", "claude::sonnet")

    assert result.returncode == 1
    payload = cli_payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"
