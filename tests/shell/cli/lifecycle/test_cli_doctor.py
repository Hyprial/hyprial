"""Failure-path tests for the `hyprial doctor` zenoh transport check.

A running daemon with no explicit Zenoh endpoints and discovery disabled is a
deterministically isolated node: it must FAIL the doctor check (not warn), so
a switch or restart never looks green while the node cannot reach anyone.
"""

from __future__ import annotations

import time

from pathlib import Path
import json
from typing import Any

import pytest


import hyprial.shell.impl.cli.commands.doctor.commands as doctor_module
import hyprial.daemon.impl.network.tailcat as tailcat_module
from hyprial.identity import (
    DeviceRecord,
    device_key_path,
    device_record_path,
    write_device_record,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.doctor.commands as _cli_owner_1
import hyprial.shell.impl.cli.commands.init.init as _cli_owner_2
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.shell.cli.lifecycle.doctor.doctor_support import (
    checks as _checks,
    doctor as _doctor,
    running_ps as _running_ps,
)


@pytest.fixture(autouse=True)
def _tailcat_checks_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep legacy daemon-IPC doctor tests focused: the tailnet-cutover local
    checks (sidecar binary, device key) are pinned separately at the bottom
    of this file."""

    monkeypatch.setattr(
        doctor_module,
        "_tailcat_sidecar_doctor_check",
        lambda: {"name": "tailcat-sidecar", "status": "ok", "detail": "stubbed"},
    )
    monkeypatch.setattr(
        doctor_module,
        "_device_key_doctor_check",
        lambda: {"name": "device-key", "status": "ok", "detail": "stubbed"},
    )
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    monkeypatch.setattr(
        service_module,
        "daemon_service_doctor_check",
        lambda **_kwargs: None,
    )


def test_zenoh_session_state_closed_is_a_fail() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    closed = _zenoh_doctor_check(
        {"zenoh": {"listen": ["tcp/x"], "connect": [], "session": {"state": "closed"}}}
    )
    assert closed["status"] == "fail"
    degraded = _zenoh_doctor_check(
        {
            "zenoh": {
                "listen": ["tcp/x"],
                "connect": [],
                "session": {
                    "state": "degraded",
                    "recoveryAttempts": 3,
                    "recoveryLastError": "TimeoutError: open timed out",
                },
            }
        }
    )
    assert degraded["status"] == "fail"
    assert "3 reopen attempts" in degraded["detail"]
    assert "TimeoutError" in degraded["detail"]
    healthy = _zenoh_doctor_check(
        {"zenoh": {"listen": ["tcp/x"], "connect": [], "session": {"state": "open"}}}
    )
    assert healthy["status"] == "ok"
    old_daemon = _zenoh_doctor_check({"zenoh": {"listen": ["tcp/x"], "connect": []}})
    assert old_daemon["status"] == "ok"


def test_zenoh_doctor_exposes_sidecar_connection_telemetry() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    telemetry = {
        "ringCapacity": 16,
        "slots": [{"peer": "dev-a", "mapPort": 41001, "closedCount": 4}],
    }
    check = _zenoh_doctor_check(
        {
            "zenoh": {"listen": [], "connect": ["tcp/127.0.0.1:41001"]},
            "forwarding": {
                "connectionTelemetry": telemetry,
                "derpMapURL": "https://derpmap.example/map.json",
                "derpMapSource": "configured",
            },
        }
    )

    assert check["status"] == "ok"
    assert check["metrics"]["connectionTelemetry"] == telemetry
    assert check["metrics"]["derpMapURL"] == "https://derpmap.example/map.json"
    assert check["metrics"]["derpMapSource"] == "configured"


def test_zenoh_rebuild_warns_only_after_the_normal_window() -> None:
    """A healthy redial is seconds; sampling it must not make doctor flap."""

    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    now = int(time.time() * 1000)

    def check(since: object) -> dict[str, object]:
        return _zenoh_doctor_check(
            {
                "zenoh": {
                    "listen": ["tcp/x"],
                    "connect": [],
                    "session": {
                        "state": "rebuilding",
                        "rebuilding": True,
                        "rebuildingSinceMs": since,
                    },
                }
            }
        )

    # An earlier rebuild that never returned is still visible as warn.
    stuck = check(now - 61_000)
    assert stuck["status"] == "warn"
    assert "stuck" in stuck["detail"]
    # Re-entry: after the session opened, the NEXT rebuild starts its own
    # clock, so the second entry is ok even though this daemon rebuilt before.
    started = check(now)
    assert started["status"] == "ok"
    assert "rebuild in flight" in started["detail"]
    assert check(now - 5_000)["status"] == "ok"
    # A rebuilding sample without a start time cannot prove freshness.
    assert check(None)["status"] == "warn"


def test_doctor_fails_when_running_daemon_has_no_zenoh_endpoints(
    monkeypatch: Any,
) -> None:
    result = _doctor(monkeypatch, _running_ps({"listen": [], "connect": []}))

    assert result["ok"] is False
    # ``unknown`` is reported alongside the other three: a check that could not
    # be asked is a distinct outcome, and folding it into ``ok`` would let an
    # uninspectable machine read as healthy.
    assert result["summary"] == {"ok": 4, "warn": 1, "fail": 1, "unknown": 0}
    checks = _checks(result)
    assert checks["daemon"]["status"] == "ok"
    zenoh = checks["zenoh"]
    assert zenoh["status"] == "fail"
    assert "cannot reach any other node" in str(zenoh["detail"])
    # The check must tell the operator how to fix it, matching the daemon
    # check-item structure.
    action = zenoh["action"]
    assert action["command"].startswith("hyprial init --listen")
    assert "description" in action


def test_doctor_passes_when_both_endpoint_sides_are_configured(
    monkeypatch: Any,
) -> None:
    result = _doctor(
        monkeypatch,
        _running_ps(
            {
                "listen": ["tcp/127.0.0.1:7447"],
                "connect": ["tcp/127.0.0.1:7448"],
            }
        ),
    )

    assert result["ok"] is True
    assert result["summary"]["fail"] == 0
    zenoh = _checks(result)["zenoh"]
    assert zenoh["status"] == "ok"
    assert zenoh["metrics"] == {
        "listen": ["tcp/127.0.0.1:7447"],
        "connect": ["tcp/127.0.0.1:7448"],
        "connectionTelemetry": {
            "state": "unavailable",
            "reason": "forwarding-absent",
        },
    }


def test_doctor_connect_only_node_is_not_isolated(monkeypatch: Any) -> None:
    # A leaf node that only connects to a peer can still reach that peer, so
    # it must not be treated as isolated.
    result = _doctor(
        monkeypatch, _running_ps({"listen": [], "connect": ["tcp/100.64.30.3:7447"]})
    )

    assert result["ok"] is True
    assert _checks(result)["zenoh"]["status"] == "ok"


def test_doctor_warns_when_daemon_omits_zenoh_reporting(monkeypatch: Any) -> None:
    result = _doctor(monkeypatch, _running_ps(None))

    assert _checks(result)["zenoh"]["status"] == "warn"


def test_doctor_warns_without_claiming_historical_node_recipients(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["historicalInboxRecipients"] = [
        {
            "recipient": "agent:op:old-node:kanban-sim",
            "currentRecipient": "agent:op:node-1:kanban-sim",
            "pending": 2,
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["historical-inbox-recipients"]
    assert check["status"] == "warn"
    assert check["metrics"] == {
        "recipients": 1,
        "pending": 2,
        "historicalRecipients": ["agent:op:old-node:kanban-sim"],
        "currentRecipients": ["agent:op:node-1:kanban-sim"],
    }
    assert "will not consume" in str(check["detail"])
    assert result["ok"] is True


def test_doctor_keeps_single_daemon_failure_when_daemon_is_down(
    monkeypatch: Any,
) -> None:
    from hyprial.kernel import ipc_errors

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise ipc_errors.DaemonUnavailableError("Harness daemon is not running")

    monkeypatch.setattr(_cli_support, "_daemon_request", unavailable)
    result = _cli_owner_1._doctor_result()

    assert result["ok"] is False
    names = list(_checks(result))
    # The local tailcat checks run without a daemon (host facts, not IPC);
    # the zenoh check is only meaningful against a running daemon.
    assert names == ["upgrade-steps", "tailcat-sidecar", "device-key", "delivery-paths", "daemon"]
    assert _checks(result)["daemon"]["status"] == "fail"


def test_doctor_fails_when_dsh_service_is_down_even_if_connector_looks_running(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["connectors"] = [
        {
            "runtime": "dsh",
            "name": "dsh-worker",
            "running": True,
            "endpoint": "http://127.0.0.1:13080",
        }
    ]

    def unreachable(_endpoint: str, **_kwargs: Any) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", unreachable)
    result = _doctor(monkeypatch, ps)

    # Mutation guard: removing the _dsh_doctor_check integration makes this
    # lookup fail instead of allowing a process-liveness false green.
    check = _checks(result)["dsh-endpoint"]
    assert result["ok"] is False
    assert check["status"] == "fail"
    assert "DSH endpoint unreachable" in str(check["detail"])
    assert "http://127.0.0.1:13080" in str(check["detail"])
    assert check["metrics"]["unreachable"] == ["http://127.0.0.1:13080"]


def test_doctor_reports_dsh_configuration_without_claiming_inference(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["connectors"] = [
        {
            "runtime": "dsh",
            "name": "dsh-worker",
            "running": True,
            "endpoint": "http://127.0.0.1:13080",
        }
    ]
    monkeypatch.setattr(
        _cli_get_services(),
        "_dsh_host_describe",
        lambda endpoint, **kwargs: {"provider": "test-provider", "model": "test-model"},
    )

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["dsh-endpoint"]
    assert check["status"] == "ok"
    assert check["metrics"] == {
        "endpoints": ["http://127.0.0.1:13080"],
        "reachable": ["http://127.0.0.1:13080"],
        "unreachable": [],
        "missingModelConfiguration": [],
        "unprobed": [],
        "missingEndpoint": [],
    }
    assert "were not tested" in check["detail"]
    assert result["ok"] is True


def test_doctor_fails_when_dsh_has_no_configured_model(monkeypatch: Any) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["connectors"] = [
        {
            "runtime": "dsh",
            "name": "dsh-worker",
            "running": True,
            "endpoint": "http://127.0.0.1:13080",
        }
    ]
    monkeypatch.setattr(_cli_get_services(), "_dsh_host_describe", lambda endpoint, **kwargs: {})

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["dsh-endpoint"]
    assert check["status"] == "fail"
    assert check["metrics"]["missingModelConfiguration"] == ["http://127.0.0.1:13080"]
    assert result["ok"] is False


def test_doctor_fails_when_lark_process_is_alive_but_inbound_is_stale(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["adapters"] = [
        {
            "provider": "lark",
            "name": "feishu-test",
            "desired": True,
            "processRunning": True,
            "online": False,
            "status": "stale",
            "streamHealth": "stale",
            "reason": "websocket-transport-stale",
            "lastEventAt": None,
            "lastTransportAt": "2026-08-10T00:00:00.000Z",
            "lastProbeAt": "2026-08-10T00:15:01.000Z",
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["lark-inbound"]
    assert check["status"] == "fail"
    assert "process is alive" in str(check["detail"])
    assert check["metrics"] == {
        "stale": ["feishu-test"],
        "unavailable": [],
        "unknown": [],
        "knownChatsRecovery": "supported",
        "unknownFirstChatRecovery": "unsupported",
        "chatEnumeration": "not-implemented",
    }


def test_doctor_reports_healthy_desired_lark_inbound(monkeypatch: Any) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["adapters"] = [
        {
            "provider": "lark",
            "name": "feishu-test",
            "desired": True,
            "processRunning": True,
            "online": True,
            "status": "online",
            "streamHealth": "healthy",
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["lark-inbound"]
    assert check["status"] == "ok"
    assert check["metrics"]["knownChatsRecovery"] == "supported"
    assert check["metrics"]["unknownFirstChatRecovery"] == "unsupported"
    assert check["metrics"]["chatEnumeration"] == "not-implemented"
    assert "known chats only" in str(check["detail"])
    assert "unknown first-chat recovery is unsupported" in str(check["detail"])
    assert result["ok"] is True


def test_doctor_warns_when_online_lark_worker_omits_health_telemetry(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["adapters"] = [
        {
            "provider": "lark",
            "name": "old-worker",
            "desired": True,
            "processRunning": True,
            "online": True,
            "status": "online",
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["lark-inbound"]
    assert check["status"] == "warn"
    assert check["metrics"]["unknown"] == ["old-worker"]
    assert result["ok"] is True


def test_doctor_warns_while_lark_history_is_being_verified(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["adapters"] = [
        {
            "provider": "lark",
            "name": "history-check",
            "desired": True,
            "processRunning": True,
            "online": False,
            "status": "checking",
            "streamHealth": "checking",
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["lark-inbound"]
    assert check["status"] == "warn"
    assert check["metrics"]["unavailable"] == []
    assert result["ok"] is True


def test_doctor_warns_for_legacy_or_unfenced_interactive_channels(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "legacy-name-must-not-be-copied-into-metrics",
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelGeneration": "legacy_or_unknown",
        },
        {
            "actor": "current-but-unfenced",
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": False,
            "channelGeneration": "current",
            "channelCurrentThisGeneration": False,
            "channelCurrentEpoch": None,
        },
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["mcp-channel-generation"]
    assert check["status"] == "warn"
    assert check["metrics"] == {
        "total": 2,
        "current": 0,
        "telemetryCurrent": 1,
        "notCurrentThisGeneration": 2,
        "legacyOrUnknown": 1,
        "unfenced": 2,
        "protocolVersion": 2,
        "recentlyObserved": 0,
        "alive": 0,
    }
    assert "legacy-name" not in str(check)
    assert "hyprial agent start claude" in check["action"]["command"]
    assert result["ok"] is True


def test_doctor_reports_current_fenced_channel_generation(monkeypatch: Any) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "not-exported",
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelBuildVersion": "0.4.1.dev0",
            "channelProtocolVersion": 2,
            "ownerFence": True,
            "channelGeneration": "current",
            "channelCurrentThisGeneration": True,
            "channelCurrentEpoch": "daemon-epoch",
            "channelRecentlyObserved": True,
            "channelAlive": True,
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["mcp-channel-generation"]
    assert check["status"] == "ok"
    assert check["metrics"]["current"] == 1
    assert check["metrics"]["telemetryCurrent"] == 1
    assert check["metrics"]["notCurrentThisGeneration"] == 0
    assert check["metrics"]["unfenced"] == 0
    assert check["metrics"]["alive"] == 1
    assert "not-exported" not in str(check)


def test_zenoh_endpoint_warning_helper() -> None:
    assert (
        _cli_owner_2._zenoh_endpoint_warning({"zenoh": {"listen": [], "connect": []}})
        is not None
    )
    assert (
        _cli_owner_2._zenoh_endpoint_warning(
            {"zenoh": {"listen": ["tcp/127.0.0.1:7447"], "connect": []}}
        )
        is None
    )
    assert _cli_owner_2._zenoh_endpoint_warning({"daemon": {"running": True}}) is None


def test_worker_cleanup_doctor_warning_names_the_failed_reclaim() -> None:
    finding = {
        "graphId": "wf-cleanup",
        "actor": "agent:test:machine:worker",
        "actorName": "worker",
        "actorNode": "_actor.worker",
        "operationId": "pac-cleanup:operation",
        "ageMs": 321,
        "reason": "marker_mismatch",
        "lastObservation": {
            "present": True,
            "identityMarker": "pac:another-process",
        },
    }

    check = _cli_owner_1._workflow_worker_cleanup_doctor_check(
        {"workflowWorkerCleanup": {"attention": [finding]}}
    )

    assert check is not None
    assert check["status"] == "warn"
    assert "graph=wf-cleanup" in check["detail"]
    assert "actor=agent:test:machine:worker" in check["detail"]
    assert "operation=pac-cleanup:operation" in check["detail"]
    assert "ageMs=321" in check["detail"]
    assert "pac:another-process" in check["detail"]
    assert check["metrics"]["workers"] == [finding]


# --------------------------------------------------------------------------
# duplicate-instance check
# --------------------------------------------------------------------------


def _ps_with_duplicate(info: object) -> dict[str, object]:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["duplicateInstance"] = info
    return ps


def test_doctor_omits_duplicate_check_against_pre_field_daemon(
    monkeypatch: Any,
) -> None:
    """A daemon old enough to not report duplicateInstance gets no check at
    all -- its absence is the honest signal, and existing summaries are
    unchanged."""

    result = _doctor(
        monkeypatch, _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    )

    assert "duplicate-instance" not in _checks(result)


def test_doctor_duplicate_check_ok_states_coverage_limit(monkeypatch: Any) -> None:
    result = _doctor(
        monkeypatch,
        _ps_with_duplicate(
            {
                "active": False,
                "meshPeerGenerations": [],
                "startupRecord": None,
                "meshDetectionCoverage": "only peers that declare a generation token",
            }
        ),
    )

    check = _checks(result)["duplicate-instance"]
    assert check["status"] == "ok"
    # The coverage limit must ride along even on green (批复①条件1): a
    # green check must never read as whole-mesh coverage.
    assert "only peers that declare a generation token" in str(check["detail"])


def test_doctor_duplicate_check_fails_loudly_when_active(monkeypatch: Any) -> None:
    result = _doctor(
        monkeypatch,
        _ps_with_duplicate(
            {
                "active": True,
                "meshPeerGenerations": ["gen-peer"],
                "startupRecord": {"recordPid": 222, "recordGeneration": "gen-old"},
                "meshDetectionCoverage": "only peers that declare a generation token",
            }
        ),
    )

    assert result["ok"] is False
    check = _checks(result)["duplicate-instance"]
    assert check["status"] == "fail"
    detail = str(check["detail"])
    assert "gen-peer" in detail
    assert "222" in detail
    assert "gen-old" in detail
    assert "no process is killed" in detail
    assert "only peers that declare a generation token" in detail
    assert check["action"]["command"] == "hyprial daemon status --json"


# -- tailnet cutover: Tailcat sidecar and device-key checks -------------------


def _record(home_owner: str = "e2e") -> DeviceRecord:
    return DeviceRecord(
        device_id="doctor-test-node",
        owner=home_owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        key_generation=1,
    )


@pytest.fixture()
def _real_tailcat_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Undo the file-wide stub (and re-isolate the home) for the checks' own
    tests."""

    monkeypatch.undo()
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(mode=0o700)
    return tmp_path / "home"


def test_tailcat_sidecar_check_warns_when_the_sidecar_is_missing(
    _real_tailcat_checks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(home, environ=None):
        raise tailcat_module.TailcatSidecarError("SIDECAR_MISSING", "no binary")

    monkeypatch.setattr(tailcat_module, "verify_tailcat_sidecar", missing)

    check = doctor_module._tailcat_sidecar_doctor_check()
    assert check["name"] == "tailcat-sidecar"
    assert check["status"] == "warn"
    assert "SIDECAR_MISSING" in str(check["detail"])
    assert check["action"]["command"] == "hyprial login"


def test_tailcat_sidecar_check_ok_when_version_verifies(
    _real_tailcat_checks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tailcat_module,
        "verify_tailcat_sidecar",
        lambda home, environ=None: {
            "v": 6,
            "sidecar": "0.1.0",
            "tailcat": tailcat_module.TAILCAT_COMMIT,
        },
    )

    check = doctor_module._tailcat_sidecar_doctor_check()
    assert check["status"] == "ok"
    assert "0.1.0" in str(check["detail"])


def test_device_key_check_warns_without_key_and_record(
    _real_tailcat_checks: Path,
) -> None:
    check = doctor_module._device_key_doctor_check()
    assert check["name"] == "device-key"
    assert check["status"] == "warn"
    assert "key file" in str(check["detail"])
    assert "device record" in str(check["detail"])
    assert check["action"]["command"] == "hyprial login"


def test_device_key_check_ok_with_key_and_record(
    _real_tailcat_checks: Path,
) -> None:
    home = _real_tailcat_checks
    key = device_key_path(home)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("{}\n", encoding="utf-8")
    key.chmod(0o600)
    write_device_record(home, _record())

    check = doctor_module._device_key_doctor_check()
    assert check["status"] == "ok"
    assert "doctor-test-node" in str(check["detail"])


def test_tailcat_sidecar_check_fails_when_the_sidecar_is_invalid(
    _real_tailcat_checks: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Present but wrong (e.g. a different Tailcat commit) is a real fault.
    def invalid(home, environ=None):
        raise tailcat_module.TailcatSidecarError("SIDECAR_INVALID", "commit mismatch")

    monkeypatch.setattr(tailcat_module, "verify_tailcat_sidecar", invalid)

    check = doctor_module._tailcat_sidecar_doctor_check()
    assert check["status"] == "fail"
    assert "SIDECAR_INVALID" in str(check["detail"])


def test_device_key_check_fails_on_an_unreadable_device_record(
    _real_tailcat_checks: Path,
) -> None:
    home = _real_tailcat_checks
    record = device_record_path(home)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{not json", encoding="utf-8")

    check = doctor_module._device_key_doctor_check()
    assert check["status"] == "fail"
    assert "unreadable" in str(check["detail"])


@pytest.mark.parametrize(
    ("forwarding", "expected"),
    [
        (
            {"connectionTelemetry": {"ringCapacity": 16, "slots": []}},
            {"ringCapacity": 16, "slots": []},
        ),
        (
            {"connectionTelemetry": {"ringCapacity": 16, "slots": [{"peer": "p"}]}},
            {"ringCapacity": 16, "slots": [{"peer": "p"}]},
        ),
        (
            {},
            {"state": "unavailable", "reason": "capability-absent"},
        ),
        (
            None,
            {"state": "unavailable", "reason": "forwarding-absent"},
        ),
    ],
)
def test_zenoh_doctor_distinguishes_telemetry_absence(
    forwarding: dict | None, expected: dict
) -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    result = {"zenoh": {"listen": [], "connect": ["tcp/127.0.0.1:41001"]}}
    if forwarding is not None:
        result["forwarding"] = forwarding
    check = _zenoh_doctor_check(result)

    assert check["metrics"]["connectionTelemetry"] == expected


def test_zenoh_doctor_never_calls_a_never_connected_peer_ok() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    result = {
        "zenoh": {"listen": [], "connect": ["tcp/127.0.0.1:41001"]},
        "forwarding": {
            "connectionTelemetry": {
                "ringCapacity": 16,
                "slots": [
                    {
                        "peer": "dev-a",
                        "dialAttempts": 4,
                        "dialFailures": 4,
                        "dialFailureReasons": {"refused": 4},
                        "splicesEstablished": 0,
                        "closedCount": 0,
                        "closeReasons": {},
                        "lastDialAtMs": 1792680000000,
                        "lastDialError": "refused",
                        "errorCounts": {},
                        "recent": [],
                    }
                ],
            }
        },
    }

    check = _zenoh_doctor_check(result)

    assert check["status"] != "ok"
    assert "never established" in check["detail"]


def test_zenoh_doctor_rebuilding_never_hides_a_never_connected_peer() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    result = {
        "zenoh": {
            "listen": [],
            "connect": ["tcp/127.0.0.1:41001"],
            "session": {
                "state": "rebuilding",
                "rebuilding": True,
                "rebuildingSinceMs": int(time.time() * 1000),
            },
        },
        "forwarding": {
            "connectionTelemetry": {
                "ringCapacity": 16,
                "slots": [
                    {
                        "peer": "dev-a",
                        "dialAttempts": 2,
                        "dialFailures": 2,
                        "splicesEstablished": 0,
                    }
                ],
            }
        },
    }

    check = _zenoh_doctor_check(result)

    assert check["status"] != "ok"
    assert "never established" in check["detail"]


def test_zenoh_doctor_four_telemetry_states_are_distinct() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _zenoh_doctor_check

    base = {"zenoh": {"listen": [], "connect": ["tcp/127.0.0.1:41001"]}}
    payloads = [
        dict(base),
        {**base, "forwarding": {}},
        {
            **base,
            "forwarding": {"connectionTelemetry": {"ringCapacity": 16, "slots": []}},
        },
        {
            **base,
            "forwarding": {
                "connectionTelemetry": {
                    "ringCapacity": 16,
                    "slots": [{"peer": "dev-a", "mapPort": 41001}],
                }
            },
        },
    ]
    checks = [_zenoh_doctor_check(payload) for payload in payloads]

    assert len({json.dumps(check, sort_keys=True) for check in checks}) == 4
    telemetry = [check["metrics"]["connectionTelemetry"] for check in checks]
    assert len({json.dumps(item, sort_keys=True) for item in telemetry}) == 4


def test_doctor_surfaces_the_persisted_start_failure_counter(
    monkeypatch: Any, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.application.start_backoff import record_start_failure

    state_dir = tmp_path / "state"
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    for _ in range(3):
        record_start_failure(state_dir, code="MIGRATION_AUTHORITY_MISSING",
                             phase="actor-runtime-forwarding-reconfigure")

    from hyprial.kernel import ipc_errors

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise ipc_errors.DaemonUnavailableError("Harness daemon is not running")

    # A's composition resolves the request port through the support seam, so
    # this patches the same object the runtime resolves per call.
    monkeypatch.setattr(_cli_support, "_daemon_request", unavailable)
    result = _cli_owner_1._doctor_result()

    check = _checks(result)["daemon-start-failures"]
    assert check["status"] == "fail"
    assert check["metrics"]["count"] == 3
    assert check["metrics"]["lastCode"] == "MIGRATION_AUTHORITY_MISSING"
    assert check["action"]["command"] == "hyprial org migration-status"
    assert "3 consecutive failed daemon start" in check["detail"]
    assert list(_checks(result))[:4] == ["upgrade-steps", "tailcat-sidecar", "device-key", "daemon-start-failures"]


def test_doctor_warns_when_channel_build_is_older_than_daemon(
    monkeypatch: Any,
) -> None:
    """A long-lived stdio channel keeps its compiled tool schema after upgrade.

    Protocol 2 predates ``summary``, so generation telemetry alone calls that
    process current.  The build token is the only signal that its MCP schema
    is old; doctor must name it and give the restart path.
    """

    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "stale-channel-process",
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelBuildVersion": "0.5.5.1",
            "channelProtocolVersion": 2,
            "channelBuildStale": True,
            "channelGeneration": "current",
            "channelCurrentThisGeneration": True,
            "channelCurrentEpoch": "daemon-epoch",
            "channelRecentlyObserved": True,
            "channelAlive": True,
            "ownerFence": True,
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["mcp-channel-generation"]
    assert check["status"] == "warn"
    assert check["metrics"]["buildStale"] == 1
    assert "different hyprial build" in check["detail"]
    assert "restart the Claude session" in check["detail"]
    assert "stale-channel-process" not in str(check)
    assert "hyprial agent start claude" in check["action"]["command"]


# --- exit status: three outcomes, precedence matters ------------------------
#
# ``10`` exists so "I could not ask" is distinguishable from "I asked and it is
# broken".  A branch that is never exercised is indistinguishable from a branch
# that is wrong, so both the 10 branch and its precedence over 1 are pinned.


def test_doctor_exit_code_prefers_failure_over_incomplete() -> None:
    from hyprial.shell.impl.cli.commands.doctor.commands import _doctor_exit_code

    assert _doctor_exit_code({"ok": 2, "warn": 0, "fail": 0, "unknown": 0}) == 0
    assert _doctor_exit_code({"ok": 1, "warn": 0, "fail": 0, "unknown": 1}) == 10
    # A real failure outranks an unanswered check: 10 must not mask a failure.
    assert _doctor_exit_code({"ok": 0, "warn": 0, "fail": 1, "unknown": 1}) == 1


def test_doctor_reports_incomplete_when_a_check_cannot_be_asked(
    monkeypatch: Any,
) -> None:
    from hyprial.shell.impl.cli.commands.daemon import service as daemon_service

    monkeypatch.setattr(
        daemon_service,
        "daemon_service_doctor_check",
        lambda **kwargs: {
            "name": "daemon-service",
            "status": "unknown",
            "detail": "the per-user systemd bus is not reachable from this session",
            "metrics": {"reason": "user-bus-unavailable"},
        },
    )

    result = _doctor(monkeypatch, _running_ps({"listen": ["tcp/x"], "connect": []}))

    assert result["summary"]["unknown"] == 1
    assert result["summary"]["fail"] == 0
    assert result["ok"] is False
    assert result["exitCode"] == 10
