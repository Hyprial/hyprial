from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.daemon.impl.squire as squire_package
from hyprial.identity import (
    RuntimeCapability,
    UserProfileError,
    UserProfileStore,
)
from hyprial.daemon.impl.squire import (
    RuntimeProber,
    build_probe_command,
    classify_run,
    probe_combinations)
from hyprial.daemon.impl.squire.probe import ProbeRun, _subprocess_runner
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

PROBED_AT = "2026-08-06T12:00:00Z"


def available(
    harness: str = "claude", model: str = "sonnet", provider: str | None = None
) -> RuntimeCapability:
    return RuntimeCapability(
        harness=harness,
        provider=provider,
        model=model,
        status="available",
        probed_at=PROBED_AT,
    )


def make_store(tmp_path: Path) -> UserProfileStore:
    store = UserProfileStore(tmp_path / "users.json")
    store.ensure(
        owner="gregpaws",
        owner_key="owner-key-1",
        login_name="gregpaws",
        machine="mac-studio",
        machine_key="machine-key-1",
    )
    return store


def legacy_profile_json() -> dict[str, Any]:
    """A pre-runtime-capability profile document (no such section)."""

    return {
        "version": 1,
        "users": [
            {
                "owner": "gregpaws",
                "ownerKey": "owner-key-1",
                "loginName": "gregpaws",
                "agents": [],
                "preferredReceiver": {
                    "machine": "mac-studio",
                    "machineKey": "machine-key-1",
                },
                "notificationRules": [],
            }
        ],
    }


# --- schema ---


def test_runtime_capability_round_trip() -> None:
    capability = RuntimeCapability(
        harness="pi",
        provider="kimi-coding",
        model="k3",
        status="unavailable",
        reason="usage-limit",
        detail="You're out of extra usage",
        retry_at="2026-08-07T10:00:00Z",
        probed_at=PROBED_AT,
    )
    parsed = RuntimeCapability.from_json(capability.to_json(), "capability")
    assert parsed == capability
    assert parsed.combo == ("pi", "kimi-coding", "k3")


def test_legacy_profile_without_runtime_capabilities_still_loads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps(legacy_profile_json()), encoding="utf-8")
    store = UserProfileStore(path)

    profile = store.get("owner-key-1")

    assert profile is not None
    assert profile.runtime_capabilities == ()
    assert "runtimeCapabilities" not in profile.to_json()


def test_resaving_legacy_profile_does_not_invent_the_section(
    tmp_path: Path,
) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps(legacy_profile_json()), encoding="utf-8")
    store = UserProfileStore(path)

    store.associate_agent("owner-key-1", "agent:gregpaws:mac-studio:squire")

    assert "runtimeCapabilities" not in path.read_text(encoding="utf-8")


def test_delivery_agent_round_trips_and_repeated_set_is_a_no_op(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)

    profile, changed = store.set_delivery_agent("owner-key-1", "owner-key-1-proxy")

    assert changed == ("users.deliveryAgent",)
    assert profile.delivery_agent == "owner-key-1-proxy"
    assert profile.to_json()["deliveryAgent"] == "owner-key-1-proxy"
    before = store.path.read_bytes()

    profile, changed = store.set_delivery_agent("owner-key-1", "owner-key-1-proxy")

    assert changed == ()
    assert store.path.read_bytes() == before
    reloaded = UserProfileStore(store.path).get("owner-key-1")
    assert reloaded is not None
    assert reloaded.delivery_agent == "owner-key-1-proxy"


@pytest.mark.parametrize(
    "document, fragment",
    [
        ({"harness": "emacs"}, "harness"),
        ({"harness": "pi", "model": "k3"}, "provider"),
        ({"harness": "claude", "model": "sonnet", "status": "busy"}, "status"),
        (
            {"harness": "claude", "model": "sonnet", "status": "unavailable"},
            "reason",
        ),
        (
            {
                "harness": "claude",
                "model": "sonnet",
                "status": "available",
                "reason": "usage-limit",
            },
            "reason",
        ),
        (
            {"harness": "claude", "model": "sonnet", "status": "available"},
            "probedAt",
        ),
    ],
)
def test_runtime_capability_validation_rejects_bad_documents(
    document: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(UserProfileError, match=fragment):
        RuntimeCapability.from_json(document, "capability")


def test_duplicate_combos_are_rejected(tmp_path: Path) -> None:
    document = legacy_profile_json()
    combo = available().to_json()
    document["users"][0]["runtimeCapabilities"] = [combo, combo]
    path = tmp_path / "users.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UserProfileError, match="duplicate"):
        UserProfileStore(path).list()


# --- store upsert and queries ---


def test_set_runtime_capability_upserts_and_reports_changes(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    first = available()

    profile, changed = store.set_runtime_capability("owner-key-1", first)
    assert changed == ("users.runtimeCapabilities",)
    assert profile.runtime_capabilities == (first,)

    _, changed = store.set_runtime_capability("owner-key-1", first)
    assert changed == ()

    refreshed = RuntimeCapability(
        harness="claude",
        model="sonnet",
        status="unavailable",
        reason="usage-limit",
        probed_at="2026-08-06T13:00:00Z",
    )
    profile, changed = store.set_runtime_capability("owner-key-1", refreshed)
    assert changed == ("users.runtimeCapabilities",)
    assert profile.runtime_capabilities == (refreshed,)

    other = available("pi", "k3", "kimi-coding")
    profile, _ = store.set_runtime_capability("owner-key-1", other)
    assert [c.combo for c in profile.runtime_capabilities] == [
        refreshed.combo,
        other.combo,
    ]

    reloaded = UserProfileStore(store.path).get("owner-key-1")
    assert reloaded is not None
    assert reloaded.runtime_capabilities == profile.runtime_capabilities


def test_set_runtime_capability_rejects_unpersistable_values(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    bogus = RuntimeCapability(
        harness="claude", model="sonnet", status="unavailable", probed_at=PROBED_AT
    )
    with pytest.raises(UserProfileError, match="reason"):
        store.set_runtime_capability("owner-key-1", bogus)


def test_availability_queries(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.set_runtime_capability("owner-key-1", available("claude", "sonnet"))
    store.set_runtime_capability(
        "owner-key-1", available("pi", "k3", "kimi-coding")
    )
    store.set_runtime_capability(
        "owner-key-1",
        RuntimeCapability(
            harness="pi",
            provider="deepseek",
            model="deepseek-flash",
            status="unavailable",
            reason="missing-credential",
            probed_at=PROBED_AT,
        ),
    )
    profile = store.get("owner-key-1")
    assert profile is not None

    assert profile.is_available("claude", "sonnet") is True
    assert profile.is_available("pi", "k3", "kimi-coding") is True
    assert profile.is_available("pi", "deepseek-flash", "deepseek") is False
    assert profile.is_available("codex", "gpt-5.3-codex") is False

    assert {c.model for c in profile.available_capabilities()} == {"sonnet", "k3"}
    assert [c.model for c in profile.available_capabilities(harness="pi")] == ["k3"]
    assert profile.available_capabilities(provider="deepseek") == ()


# --- probe command construction and classification ---


def test_build_probe_command_uses_real_invocations() -> None:
    assert build_probe_command("pi", "k3", "kimi-coding") == (
        "pi",
        "-p",
        "--provider",
        "kimi-coding",
        "--model",
        "k3",
        "--thinking",
        "high",
        "hello",
    )
    assert build_probe_command("claude", "sonnet") == (
        "claude",
        "-p",
        "--model",
        "sonnet",
        "hello",
    )
    assert build_probe_command("codex", "gpt-5.3-codex") == (
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--model",
        "gpt-5.3-codex",
        "hello",
    )


def test_build_probe_command_requires_provider_for_pi() -> None:
    with pytest.raises(ValueError, match="provider"):
        build_probe_command("pi", "k3")
    with pytest.raises(ValueError, match="unsupported harness"):
        build_probe_command("emacs", "k3")


def test_classify_run_success() -> None:
    assert classify_run(ProbeRun(0, "Hello!")) == ("available", None, None)


def test_classify_run_usage_limit_without_retry_hint() -> None:
    status, reason, retry_at = classify_run(
        ProbeRun(1, "You're out of extra usage for this period")
    )
    assert (status, reason, retry_at) == ("unavailable", "usage-limit", None)


def test_classify_run_usage_limit_captures_retry_at() -> None:
    output = (
        "ERROR: You've hit your usage limit. try again at 2026-08-07T10:30:00Z."
    )
    status, reason, retry_at = classify_run(ProbeRun(1, output))
    assert status == "unavailable"
    assert reason == "usage-limit"
    assert retry_at == "2026-08-07T10:30:00Z"


def test_classify_run_retry_at_kept_raw_when_unparseable() -> None:
    _, _, retry_at = classify_run(
        ProbeRun(1, "usage limit reached, try again at tomorrow morning")
    )
    assert retry_at == "tomorrow morning"


def test_classify_run_missing_credential() -> None:
    status, reason, _ = classify_run(
        ProbeRun(1, "Error: 401 Unauthorized: invalid api key")
    )
    assert (status, reason) == ("unavailable", "missing-credential")


def test_classify_run_command_not_found_and_timeout() -> None:
    assert classify_run(ProbeRun(None, "command not found: pi"))[:2] == (
        "unavailable",
        "command-not-found",
    )
    assert classify_run(ProbeRun(None, "", timed_out=True))[:2] == (
        "unavailable",
        "probe-timeout",
    )


def test_classify_run_generic_failure() -> None:
    status, reason, _ = classify_run(ProbeRun(2, "traceback: something exploded"))
    assert (status, reason) == ("unavailable", "probe-error")


def test_subprocess_runner_reports_missing_command() -> None:
    run = _subprocess_runner(("hyprial-definitely-not-a-real-binary",), 1.0)
    assert run.exit_code is None
    assert run.timed_out is False
    assert "command not found" in run.output


def test_subprocess_runner_reports_timeout() -> None:
    run = _subprocess_runner(
        (sys.executable, "-c", "import time; time.sleep(30)"), 0.2
    )
    assert run.timed_out is True


# --- prober and store orchestration ---


def test_prober_records_facts_with_injected_clock() -> None:
    calls: list[tuple[tuple[str, ...], float]] = []

    def runner(command: tuple[str, ...], timeout: float) -> ProbeRun:
        calls.append((command, timeout))
        return ProbeRun(0, "hi")

    prober = RuntimeProber(
        runner=runner,
        timeout=42.0,
        now=lambda: datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    result = prober.probe("pi", "k3", "kimi-coding")

    assert calls == [(build_probe_command("pi", "k3", "kimi-coding"), 42.0)]
    assert result.status == "available"
    assert result.probed_at == "2026-08-06T12:00:00Z"
    assert result.reason is None


def test_prober_records_failure_detail() -> None:
    prober = RuntimeProber(
        runner=lambda command, timeout: ProbeRun(1, "You're out of extra usage"),
    )
    result = prober.probe("claude", "opus")
    assert result.status == "unavailable"
    assert result.reason == "usage-limit"
    assert result.detail == "You're out of extra usage"


def test_probe_combinations_persists_unless_dry_run(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    prober = RuntimeProber(runner=lambda command, timeout: ProbeRun(0, "ok"))

    results, changed, profile = probe_combinations(
        store,
        "owner-key-1",
        [("claude", None, "sonnet"), ("pi", "kimi-coding", "k3")],
        prober=prober,
    )
    assert len(results) == 2
    assert changed == ("users.runtimeCapabilities",) * 2
    assert len(profile.runtime_capabilities) == 2

    results, changed, profile = probe_combinations(
        store,
        "owner-key-1",
        [("codex", None, "gpt-5.3-codex")],
        prober=prober,
        dry_run=True,
    )
    assert changed == ()
    assert "codex" not in {c.harness for c in profile.runtime_capabilities}
    reloaded = UserProfileStore(store.path).get("owner-key-1")
    assert reloaded is not None
    assert "codex" not in {c.harness for c in reloaded.runtime_capabilities}


def test_probe_combinations_unknown_owner_fails(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    prober = RuntimeProber(runner=lambda command, timeout: ProbeRun(0, "ok"))
    with pytest.raises(UserProfileError, match="not registered"):
        probe_combinations(
            store, "nobody", [("claude", None, "sonnet")], prober=prober
        )


# --- CLI end to end ---


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
    (state / "users.json").write_text(
        json.dumps(legacy_profile_json()), encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    return home, state, bin_dir


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
    assert payload["changed"] == ["users.runtimeCapabilities"]
    (entry,) = payload["results"]
    assert entry["harness"] == "claude"
    assert "provider" not in entry
    assert entry["model"] == "sonnet"
    assert entry["status"] == "available"
    assert entry["probedAt"]

    stored = json.loads((state / "users.json").read_text(encoding="utf-8"))
    (persisted,) = stored["users"][0]["runtimeCapabilities"]
    assert persisted["status"] == "available"

    again = run_probe(home, str(bin_dir))
    assert again.returncode == 0, again.stderr
    assert [entry["model"] for entry in cli_payload(again)["results"]] == ["sonnet"]


def test_cli_probe_dry_run_does_not_write(tmp_path: Path) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    write_executable(bin_dir, "claude", "#!/bin/sh\necho hello\n")
    before = (state / "users.json").read_bytes()

    result = run_probe(
        home, str(bin_dir), "--combo", "claude::sonnet", "--dry-run"
    )

    assert result.returncode == 0, result.stderr
    payload = cli_payload(result)
    assert payload["dryRun"] is True
    assert payload["results"][0]["status"] == "available"
    assert (state / "users.json").read_bytes() == before


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
    stored = json.loads((state / "users.json").read_text(encoding="utf-8"))
    assert stored["users"][0]["runtimeCapabilities"][0]["reason"] == "usage-limit"


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


def test_cli_probe_reports_delivery_agent_and_live_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = make_store(tmp_path)
    profile, _changed = store.set_delivery_agent(
        "owner-key-1", "owner-key-1-proxy"
    )
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, _params: {
            "connectors": [
                {
                    "runtime": "user-proxy",
                    "name": "owner-key-1-proxy",
                    "running": True,
                }
            ]
        }
        if method == "ps"
        else {},
    )
    monkeypatch.setattr(
        squire_package,
        "probe_combinations",
        lambda *_args, **_kwargs: ((), (), profile),
    )

    result = CliRunner().invoke(
        _cli_root_app,
        ["agent", "tiers", "probe", "--combo", "claude::sonnet", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["profile"]["deliveryAgent"] == "owner-key-1-proxy"
    assert payload["profile"]["deliveryAgentLive"] is True


def test_cli_probe_requires_owner_key_with_multiple_profiles(
    tmp_path: Path,
) -> None:
    home, state, bin_dir = prepare_cli_home(tmp_path)
    document = legacy_profile_json()
    second = dict(document["users"][0])
    second["ownerKey"] = "owner-key-2"
    second["preferredReceiver"] = dict(second["preferredReceiver"])
    document["users"].append(second)
    (state / "users.json").write_text(json.dumps(document), encoding="utf-8")

    result = run_probe(home, str(bin_dir), "--combo", "claude::sonnet")

    assert result.returncode == 1
    assert "--owner-key" in cli_payload(result)["error"]
