"""The CPU budget alarm must name the actor that ran the #1173 hot path.

#1173: every heartbeat ``BindAgentCommand`` resolved each supervisor row's
canonical URI with a whole desired-state load, on agent-registry-authority.
That actor keeps its legacy ``agentActor`` ipcStats side and is excluded from
the generic ``runtime`` block, so an alarm fed by ``runtime`` alone could never
name it.  This replays the pre-#1173 method verbatim through the real Agent
actor and the production maintenance check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.kernel import canonical_agent_uri
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.state_persistence.alarm import CpuOwnerBudgetAlarm
from hyprial.identity.impl.agents.actor.ports import (
    AgentMutationCompleted,
    BindAgentCommand,
)
from tests.daemon.scaling.test_identity_session_cost_contracts import (
    _application,
    _seed_supervisor,
)

ROWS = 48
BINDS = 20
WINDOW_S = 30.0


class _Pre1173:
    """``_managed_worker_running`` and ``_canonical_harness_uri`` copied
    verbatim from db85f0bc: the per-row whole-document load is in the second.
    """

    def _canonical_harness_uri(
        self,
        name: str,
        spec: HarnessLaunchSpec | None = None,
        desired: DesiredState | None = None,
    ) -> str:
        """The network identity this daemon mints for a managed harness.

        A transferred or host-invited spec carries ``pinned_owner``: the
        URI keeps the admitted owner instead of this daemon's ambient one,
        so hosting never silently renames the worker. The machine
        segment stays this node's id -- the worker physically lives here.

        The spec=None fallback is a defensive load for single-call paths
        (e.g. identity.whoami).  Batch paths must pass the spec they already
        hold: per-iteration fallback loads are what turned one ps into
        O(agents × connectors) full desired-state reads.  ``desired`` is the
        caller's already-loaded state (card 259) for callers that hold no
        spec but do hold the state -- a snapshot hands its one copy down
        instead of every row re-reading the store.  The URI it returns is
        the same either way.
        """

        owner = self.owner
        if spec is None and desired is None:
            # A batch path that installed the worker-status snapshot already
            # holds one loaded copy; reuse it instead of one full load per
            # call (the runtime timer resolved every streaming actor this way,
            # 2026-09-25 production dump on pid 54206).
            snapshot = self._current_worker_snapshot()
            if snapshot is not None and snapshot.desired is not None:
                desired = snapshot.desired
        if spec is None:
            try:
                state = (
                    self.desired_state.load() if desired is None else desired
                )
                spec = next(
                    (item for item in state.harnesses if item.name == name),
                    None,
                )
            except Exception:  # noqa: BLE001 - identity must never crash
                spec = None
        if spec is not None and spec.pinned_owner:
            owner = spec.pinned_owner
        return canonical_agent_uri(owner, self.node_id, name)

    def _managed_worker_running(
        self, actor: str, desired: DesiredState | None = None
    ) -> bool | None:
        """Supervised-subprocess state for a daemon-managed worker, else None.

        This is the signal ``connectors[].running`` already reported on its
        own; feeding it into AgentLiveness is what collapses two of the three
        old models into one instead of adding a fourth.  ``desired`` is the
        snapshot's already-loaded desired state (card 259), threaded to
        ``_canonical_harness_uri`` so each row stops re-reading the store.
        """

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.running_by_actor.get(actor)
        # Single-call paths (identity.whoami, session fencing) reach here
        # without a request snapshot and keep the live per-call query.
        # Batch paths must install _worker_status_snapshot instead: every
        # call below costs one supervisor round trip plus one desired-state
        # load per connector.
        # Unit-level application.handle seams stand in a bare object for the
        # supervisor; absent a real one there is simply no worker signal.
        report = getattr(self._harnesses, "status", None)
        if not callable(report):
            return None
        statuses = report()
        if not isinstance(statuses, tuple | list):
            return None
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            if (
                self._canonical_harness_uri(name, desired=desired) == actor
                or name == actor
            ):
                return bool(status.get("running"))
        return None


def _bind_window(root: Path, *, pre_1173: bool, monkeypatch: pytest.MonkeyPatch):
    """Run BINDS exact rebinds; return (agent CPU ms, events, runtime-only events)."""

    if pre_1173:
        for method in ("_managed_worker_running", "_canonical_harness_uri"):
            monkeypatch.setattr(DaemonApplication, method, getattr(_Pre1173, method))
    else:
        monkeypatch.undo()
    app = _application(root)
    try:
        name = _seed_supervisor(app, ROWS)
        actor_uri = app.agents.create(name).uri
        domains = app._agent_session_domains
        domains.call_agent(
            BindAgentCommand("bind-first", actor_uri, "pi", "headless", "s1"),
            AgentMutationCompleted,
        )
        now = [0.0]
        events: list[dict[str, Any]] = []
        app._log = lambda _level, _source, event, **fields: (  # type: ignore[method-assign]
            events.append({"event": event, **fields})
        )
        app._cpu_owner_alarm = CpuOwnerBudgetAlarm(  # type: ignore[attr-defined]
            window_seconds=WINDOW_S, share=1.0, clock=lambda: now[0]
        )
        before = app._ipc_stats_snapshot()
        app._check_cpu_owner_budget()
        for index in range(BINDS):
            domains.call_agent(
                BindAgentCommand(
                    f"bind-{index}", actor_uri, "pi", "headless", "s1"
                ),
                AgentMutationCompleted,
            )
        after = app._ipc_stats_snapshot()
        cpu_ms = sum(r["threadCpuMs"] for r in after["agentActor"].values()) - sum(
            r["threadCpuMs"] for r in before["agentActor"].values()
        )
        return app, now, events, cpu_ms
    except BaseException:
        app._close()
        raise


def _alarm_at(app, now, events, share: float) -> list[dict[str, Any]]:
    app._cpu_owner_alarm._share = share
    now[0] = WINDOW_S
    app._check_cpu_owner_budget()
    return [e for e in events if e["event"] == "daemon.cpu.owner_over_budget"]


def test_pre_1173_bind_storm_alarms_on_agent_registry_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, now, events, fixed_ms = _bind_window(
        tmp_path / "fixed", pre_1173=False, monkeypatch=monkeypatch
    )
    try:
        # Budget calibrated just above the fixed code's own cost: the fixed
        # path must stay quiet under it.
        share = max(3 * fixed_ms, 1.0) / (WINDOW_S * 1000)
        quiet = _alarm_at(app, now, events, share)
        assert "agent-registry-authority" not in [a["owner"] for a in quiet]
    finally:
        app._close()

    app, now, events, old_ms = _bind_window(
        tmp_path / "pre-1173", pre_1173=True, monkeypatch=monkeypatch
    )
    try:
        # One whole desired-state load per supervisor row per bind.
        assert old_ms > 3 * max(fixed_ms, 1.0 / 3)
        alarms = _alarm_at(app, now, events, share)
    finally:
        app._close()
    owners = [alarm["owner"] for alarm in alarms]
    assert "agent-registry-authority" in owners, (fixed_ms, old_ms, alarms)
    alarm = alarms[owners.index("agent-registry-authority")]
    assert alarm["topWorkTypes"][0]["workType"].startswith("BindAgentCommand")
