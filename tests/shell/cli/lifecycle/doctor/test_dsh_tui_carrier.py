"""Doctor `dsh-tui-carrier` check: interactive DSH session fencing.

The carrier check warns while an interactive DSH session is not fenced by a
live process identity, and warns again once a fenced registration's process
is gone or its pid was reused.  Split from ``test_cli_doctor.py`` when the
zenoh diagnostic expectations and these fence expectations crossed the
1000-line module limit together in the #1558 dev refresh.
"""

import os
from typing import Any

from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
import hyprial.shell.impl.cli.commands.doctor.commands as _cli_owner_1


def _running_ps(zenoh: dict[str, object] | None) -> dict[str, object]:
    return {"daemon": {"running": True}, "zenoh": zenoh}


def _doctor(monkeypatch: Any, ps_result: dict[str, object]) -> dict[str, object]:
    def request(method: str, *_args: Any, **_kwargs: Any) -> object:
        if method == "agent.list":
            return {"agents": []}
        if method == "adapter.list":
            return {"adapters": []}
        return ps_result

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = _cli_owner_1._doctor_result()
    assert isinstance(result, dict)
    return result


def _checks(result: dict[str, object]) -> dict[str, dict[str, object]]:
    checks = result["checks"]
    assert isinstance(checks, list)
    return {item["name"]: item for item in checks if isinstance(item, dict)}


def test_doctor_warns_for_unfenced_interactive_dsh_sessions(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "agent:op:node:dsh",
            "source": "dsh-channel",
            "runtime": "dsh_interactive",
            "channelConfirmed": True,
            "online": True,
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["dsh-tui-carrier"]
    assert check["status"] == "warn"
    assert check["metrics"] == {
        "total": 1,
        "online": 1,
        "unfenced": 1,
        "processGone": 0,
    }
    assert "hyprial agent start dsh" in check["action"]["command"]


def test_doctor_reports_fenced_interactive_dsh_sessions(
    monkeypatch: Any,
) -> None:
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "agent:op:node:dsh",
            "source": "dsh-channel",
            "runtime": "dsh_interactive",
            "channelConfirmed": True,
            "online": True,
            "processPid": os.getpid(),
            "processIdentity": _live_identity(os.getpid()),
        }
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["dsh-tui-carrier"]
    assert check["status"] == "ok"
    assert check["metrics"]["unfenced"] == 0
    assert check["metrics"]["processGone"] == 0


def _live_identity(pid: int) -> str:
    from hyprial.daemon import process_identity_for_tui

    identity = process_identity_for_tui(pid)
    assert identity
    return identity


def _stale_identity(pid: int) -> str:
    """Same schemes as the live marker, every value different: pid reuse."""

    return ";".join(
        f"{piece.partition(':')[0]}:stale-{index}"
        for index, piece in enumerate(_live_identity(pid).split(";"))
    )


def test_doctor_warns_when_a_fenced_dsh_tui_process_is_gone(
    monkeypatch: Any,
) -> None:
    """channelConfirmed is set for every carrier source and says nothing; a
    registration whose fenced TUI died or whose pid was reused is stale."""

    import subprocess
    import sys

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    ps = _running_ps({"listen": ["tcp/127.0.0.1:7447"], "connect": []})
    ps["interactiveSessions"] = [
        {
            "actor": "agent:op:node:dsh-dead",
            "source": "dsh-channel",
            "runtime": "dsh_interactive",
            "channelConfirmed": True,
            "online": True,
            "processPid": finished.pid,
            "processIdentity": f"{finished.pid}:gone",
        },
        {
            "actor": "agent:op:node:dsh-reused",
            "source": "dsh-channel",
            "runtime": "dsh_interactive",
            "channelConfirmed": True,
            "online": True,
            "processPid": os.getpid(),
            "processIdentity": _stale_identity(os.getpid()),
        },
    ]

    result = _doctor(monkeypatch, ps)

    check = _checks(result)["dsh-tui-carrier"]
    assert check["status"] == "warn"
    assert check["metrics"] == {
        "total": 2,
        "online": 2,
        "unfenced": 0,
        "processGone": 2,
    }
    assert "no longer running" in check["detail"]
