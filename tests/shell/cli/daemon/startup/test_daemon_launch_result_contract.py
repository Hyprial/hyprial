"""The daemon-launch result contract: one type, three readers (PR #332 F2).

F2's failure mode was not one bad read — it was that the launch answer had
no owner.  ``_launch_daemon_process`` returned a flat dict, ``upgrade`` dug
for the nested ps shape it used to return, and the test stub hand-wrote
whatever shape made that reader happy, so the suite stayed green while every
real upgrade failed with a false alert.

These tests pin the contract from the producer side: the REAL
``_launch_daemon_process`` runs over a fake ping layer (spec: "或用真实
_launch_daemon_process + 假 ping") and its answer must be the typed
``DaemonLaunchResult`` whose flat JSON form all three readers — ``init``,
``upgrade``, and the e2e runner's extracted readiness read — can parse.  A
stub that invents its own shape cannot reproduce this, which is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

# Mirror the runner import convention (tests/test_e2e_runner_teardown.py):
# the scenario helpers live outside the package and put their own directory
# on sys.path.
_SCENARIOS = Path(__file__).resolve().parents[5] / 'contract/e2e'
if str(_SCENARIOS) not in sys.path:
    sys.path.insert(0, str(_SCENARIOS))

from _daemon_runner import launch_result_is_ready  # noqa: E402

# The daemon's ping answer, field-for-field as the ping handler builds it
# (daemon/application.py, `handle("ping")`): flat, with running/pid/epoch/
# phase plus the identity fields that ride along.
PING: dict[str, Any] = {
    "running": True,
    "pid": 5201,
    "epoch": "epoch-7",
    "nodeId": "mac-studio",
    "owner": "allen",
    "socket": "/tmp/contract-daemon.sock",
    "phase": "serving",
    "restorePending": False,
    "zenoh": {"listen": ["tcp/127.0.0.1:7447"], "connect": []},
}


class _FakeProcess:
    """Stands in for the detached child; alive for every poll()."""

    pid = 5201

    def poll(self) -> int | None:
        return None


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_ready_wait_accepts_phase_progress_beyond_fifteen_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous startup progress resets the idle window, not the hard cap."""

    clock = _FakeClock()
    phases = (
        "actor-runtime-agents-store",
        "actor-runtime-transport",
        "actor-runtime-inbox-store",
        "actor-runtime-lifecycle-store",
        "ipc-server",
    )
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(
        _cli_get_services(), "_daemon_ready_for_process", lambda _pid, **_kw: clock.now >= 16.5
    )
    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", lambda timeout=0.5: dict(PING))

    def progress() -> dict[str, Any]:
        index = min(int(clock.now // 4.0), len(phases) - 1)
        return {"phasesSeen": index + 1, "lastStartupPhase": phases[index]}

    result = _cli_get_services()._wait_for_daemon(
        timeout=5.0,
        hard_cap=30.0,
        process=_FakeProcess(),
        startup_progress=progress,
    )

    assert result["running"] is True
    assert clock.now >= 16.5


def test_ready_wait_rejects_a_stuck_phase_after_the_idle_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unchanged phase is silence and the timeout names that phase."""

    clock = _FakeClock()
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda _pid, **_kw: False)

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._wait_for_daemon(
            timeout=2.0,
            hard_cap=20.0,
            process=_FakeProcess(),
            startup_progress=lambda: {
                "phasesSeen": 1,
                "lastStartupPhase": "actor-runtime-transport",
            },
        )

    assert caught.value.code == ipc_errors.DAEMON_START_TIMEOUT
    assert "actor-runtime-transport" in str(caught.value)
    assert 2.0 <= clock.now < 2.1


def test_ready_wait_hard_cap_still_ends_continuous_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Progress cannot extend the absolute startup ceiling."""

    clock = _FakeClock()
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda _pid, **_kw: False)

    def progress() -> dict[str, Any]:
        phase = (
            "actor-runtime-inbox-store"
            if int(clock.now * 2) % 2
            else "actor-runtime-transport"
        )
        return {
            "phasesSeen": int(clock.now * 2) + 1,
            "lastStartupPhase": phase,
        }

    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._wait_for_daemon(
            timeout=1.0,
            hard_cap=3.0,
            process=_FakeProcess(),
            startup_progress=progress,
        )

    assert caught.value.code == ipc_errors.DAEMON_START_TIMEOUT
    assert "hard cap" in str(caught.value)
    assert 3.0 <= clock.now < 3.1


def test_ready_wait_caps_each_probe_at_the_remaining_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unsupervised loop shares the same absolute wall-time rule."""

    clock = _FakeClock()
    probe_timeouts: list[float] = []
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda _pid, **_kw: True)

    def slow_probe(timeout: float = 0.5) -> dict[str, Any]:
        probe_timeouts.append(timeout)
        clock.sleep(timeout)
        raise ipc_errors.DaemonUnavailableError("not ready")

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", slow_probe)

    with pytest.raises(_cli_support.CliError, match="hard cap"):
        _cli_get_services()._wait_for_daemon(
            timeout=10.0,
            hard_cap=0.6,
            process=_FakeProcess(),
        )

    assert clock.now == pytest.approx(0.6)
    assert probe_timeouts == pytest.approx([0.5, 0.05])


def test_real_legacy_probe_shares_one_wall_clock_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ping and the legacy ps fallback share one absolute request budget."""

    clock = _FakeClock()
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(_cli_get_services(), "time", clock)

    def legacy_transport(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del params
        calls.append((method, timeout))
        if method == "ping":
            clock.sleep(timeout * 0.6)
            raise _cli_support.CliError(
                ipc_errors.METHOD_NOT_FOUND,
                "old daemon does not implement ping",
            )
        assert method == "ps"
        clock.sleep(timeout)
        return {"daemon": dict(PING)}

    monkeypatch.setattr(_cli_support, "_daemon_request", legacy_transport)

    result = _cli_get_services()._daemon_probe(timeout=1.0)

    assert result == {"daemon": PING}
    assert [method for method, _timeout in calls] == ["ping", "ps"]
    assert [timeout for _method, timeout in calls] == pytest.approx([1.0, 0.4])
    assert clock.now == pytest.approx(1.0)


def test_unsupervised_legacy_probe_cannot_double_the_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0.2s launch cap also bounds ping plus the legacy ps fallback."""

    clock = _FakeClock()
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(_cli_get_services(), "time", clock)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda _pid, **_kw: True)

    def timed_out_legacy_transport(
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del params
        calls.append((method, timeout))
        clock.sleep(timeout)
        if method == "ping":
            raise _cli_support.CliError(
                ipc_errors.METHOD_NOT_FOUND,
                "old daemon does not implement ping",
            )
        assert method == "ps"
        raise ipc_errors.DaemonUnavailableError("legacy daemon is not ready")

    monkeypatch.setattr(_cli_support, "_daemon_request", timed_out_legacy_transport)

    with pytest.raises(_cli_support.CliError, match="hard cap"):
        _cli_get_services()._wait_for_daemon(
            timeout=10.0,
            hard_cap=0.2,
            process=_FakeProcess(),
        )

    assert clock.now <= 0.2 + 0.5
    assert sum(timeout for _method, timeout in calls) <= 0.2
    assert [method for method, _timeout in calls] == ["ping"]
    assert [timeout for _method, timeout in calls] == pytest.approx([0.2])


def _pin_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)


def _real_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, probe: dict[str, Any] | None
) -> DaemonLaunchResult:
    """Run the REAL ``_launch_daemon_process`` over a fake ping layer.

    Everything else is real: the launch lock, the per-launch capture inode,
    the environment shaping.  Only the child process and the daemon's IPC
    answers are faked — a stub of the launch function itself could invent
    any shape, and inventing shapes is exactly what F2 buried.
    """

    _pin_environment(monkeypatch, tmp_path)
    if probe is None:
        # Fresh-start path: the optimistic probe misses, the launch wait's
        # ping answers.
        # The launch wait goes through the same probe now (one probe path
        # for every daemon generation), so the fake is stateful: the first,
        # optimistic call misses; every call after the launch answers.
        state = {"launched": False}

        def probe(timeout: float = 0.5) -> dict[str, Any]:
            if not state["launched"]:
                state["launched"] = True
                raise ipc_errors.DaemonUnavailableError("no daemon for contract run")
            return dict(PING)

        monkeypatch.setattr(_cli_get_services(), "_daemon_probe", probe)
        monkeypatch.setattr(
            _cli_support,
            "_daemon_request",
            lambda method, params=None, *, timeout=15.0: dict(PING),
        )
    else:
        # Already-running path: the probe under the launch lock finds it.
        monkeypatch.setattr(_cli_get_services(), "_daemon_probe", lambda timeout=0.5: dict(probe))
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda pid, **_kw: True)
    monkeypatch.setattr(_cli_subprocess, "Popen", lambda *args, **kwargs: _FakeProcess())
    return _cli_get_services()._launch_daemon_process(ready_timeout=5.0)


def test_real_launcher_returns_the_typed_result_with_a_flat_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _real_launch(monkeypatch, tmp_path, probe=None)

    assert isinstance(result, DaemonLaunchResult)
    assert result.ok is True
    assert result.started is True
    assert result.already_running is False
    assert result.running is True
    assert result.pid == PING["pid"]
    assert result.epoch == PING["epoch"]
    assert result.phase == PING["phase"]

    payload = result.to_payload()
    # FLAT: launch verdict and every ping key at the top level.  The nested
    # ps shape ("daemon") must not reappear — its return is F2's misread.
    assert payload["ok"] is True
    assert payload["started"] is True
    assert payload["alreadyRunning"] is False
    for key, value in PING.items():
        assert payload[key] == value
    assert "daemon" not in payload


def test_real_launcher_already_running_branch_builds_the_same_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = _real_launch(monkeypatch, tmp_path, probe=PING)

    assert isinstance(result, DaemonLaunchResult)
    assert result.started is False
    assert result.already_running is True
    assert result.running is True
    assert result.pid == PING["pid"]
    payload = result.to_payload()
    assert payload["alreadyRunning"] is True
    assert payload["running"] is True
    assert payload["pid"] == PING["pid"]


def test_the_e2e_runner_reads_the_typed_payload_flat() -> None:
    """Third reader (adjudication ④): the runner parses the type's form.

    ``contract/e2e/_daemon_runner.py`` reads ``hyprial init --json``
    output after it crosses the process boundary, so this is the only place
    the cross-process shape is pinned against the in-process type.
    """

    payload = DaemonLaunchResult.launched(PING).to_payload()

    assert launch_result_is_ready(payload) is True
    # The fields the runner reads by name sit at the top level, flat.
    assert payload["running"] is True
    assert payload["pid"] == PING["pid"]
    assert payload["phase"] == PING["phase"]


def test_legacy_ps_probe_answers_still_yield_typed_reads() -> None:
    """A pre-ping daemon's ps answer nests; the named reads see through it.

    ``_daemon_probe`` falls back to ``ps`` for daemons older than the ping
    contract.  Defining the type only for the two CLI callers would let
    those answers turn the named fields into silent ``None``\\s.
    """

    legacy = {"daemon": dict(PING)}
    result = DaemonLaunchResult.existing(legacy)

    assert result.running is True
    assert result.pid == PING["pid"]
    assert result.epoch == PING["epoch"]


def test_wait_for_daemon_reaches_a_pre_ping_daemon_through_the_probe(monkeypatch, tmp_path) -> None:
    """A new CLI waiting on a daemon older than the ping contract.

    ``_daemon_probe`` already falls back to ``ps`` for exactly that peer, but
    ``_wait_for_daemon`` pinged directly and let METHOD_NOT_FOUND escape:
    E2E-010 box 1 on intg 2772dea0 ("unknown daemon method ping") -- the
    scenario's seeded old build is the tree with pre-liveness daemon seams,
    i.e. a pre-ping daemon under this CLI.  Production meets the same pair
    between `uv tool install` and the daemon restart.  One probe path.
    """
    from hyprial.kernel import ipc_errors

    calls: list[str] = []

    def fake_request(method: str, params=None, *, timeout: float = 15.0, **_kw):
        calls.append(method)
        if method == "ping":
            raise _cli_get_services().CliError(ipc_errors.METHOD_NOT_FOUND, "unknown daemon method ping")
        if method == "ps":
            return {"daemon": {"running": True, "pid": 4242, "epoch": "old"}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", fake_request)
    monkeypatch.setattr(_cli_get_services(), "_daemon_ready_for_process", lambda pid, **_kw: True)

    result = _cli_get_services()._wait_for_daemon(timeout=2.0)

    assert calls[:2] == ["ping", "ps"]
    assert _cli_owner_1._probe_reports_running(result) is True
