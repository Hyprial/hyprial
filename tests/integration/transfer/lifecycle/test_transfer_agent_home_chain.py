"""Orchestrator tests: the agent-home P2 chain an AT07 ③-4 worker takes.

Same seam as its sibling module -- the remote runner is the only fake, daemon
answers come from a scripted ``local_request`` -- but here the subject is the
branch that replaced the agent-home refusal: the chain has to open for a
harness a two-daemon run proved, speak the daemon's own refusal for one it did
not, and roll a failed chain back through the audited override (never by
dropping the persona or destroying the agent).

The chain lives in ``agent_home_chain``; the rules this module pins are the
ones the adversarial review of the first landed version demanded:

* a failure before the receive call rolls the source back;
* an unresolved receive keeps the hold (the target may already have landed);
* the target's staging directory is created before the ship and removed as
  soon as nothing can be reading it;
* the quiesce round trip outlasts the daemon's own budget;
* a completion that leaves the source held is not reported as ``ok``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity.impl.agents import AgentConfig
from hyprial.daemon.impl.transfer.orchestrator import TransferError
from hyprial.daemon.impl.transfer.archive.signature import (
    public_key_id,
    read_signing_key,
    signing_key_path,
)
import hyprial.daemon.impl.transfer.archive.attribution as attribution_module
from tests.integration.transfer.lifecycle.test_transfer_orchestrator import (
    OWNER,
    SOURCE,
    TARGET,
    FakeLocal,
    FakeRemote,
    _run,
)

REPO = Path(__file__).resolve().parents[4]


class _Refusal(Exception):
    """A daemon-owned refusal, carrying the code and the data the CLI shows."""

    def __init__(self, code: str, data: dict[str, Any]) -> None:
        self.code = code
        self.data = data
        super().__init__(code)


class _CopyingRemote(FakeRemote):
    """A remote double whose worktree sync really copies (content identity).

    ``RemoteRunner.sync_tree``'s protocol is ``rsync -a`` -- additive, never
    deletes.  The plain ``FakeRemote`` only records the call, so it can pin
    the call surface but not that the source tree's *content* is routed to
    the destination cwd.  This double performs the additive copy for the one
    path under test (``shutil.copytree(..., dirs_exist_ok=True)`` is the
    closest local stand-in for ``rsync -a``); the bundle sync is still just
    recorded so the double never invents a remote staging path.
    """

    def __init__(self, *, source_cwd: Path) -> None:
        super().__init__()
        self._source_cwd = Path(source_cwd)

    def sync_tree(self, local_dir: Path, remote_dir: str) -> None:
        super().sync_tree(local_dir, remote_dir)
        if Path(local_dir) == self._source_cwd:
            shutil.copytree(local_dir, Path(remote_dir), dirs_exist_ok=True)


def _agent_home_plan(tmp_path: Path, harness: str = "pi") -> dict[str, Any]:
    config_source = tmp_path / "config"
    config_source.mkdir(exist_ok=True)
    return AgentConfig(str(config_source)).to_json()


def _seed_agent_home(home: Path, name: str = "wk") -> Path:
    agent_home = home / "agents" / name
    (agent_home / "config").mkdir(parents=True)
    (agent_home / "config" / "settings.json").write_text(
        '{"harness": "pi"}\n', encoding="utf-8"
    )
    return agent_home


def _chain_quiesce(
    local: FakeLocal,
    *,
    state: str = "quiesced",
    agent_home: Path,
    signing_home: Path | None = None,
) -> None:
    local.quiesce = {
        **local.plan,
        "stopped": state == "quiesced",
        "chain": {
            "operationId": "transfer-p2:op",
            "targetNode": TARGET,
            "targetActor": "wk",
            "planDigest": "a" * 64,
            "quiesce": {"state": state, "reason": None if state == "quiesced" else "stop-not-proven"},
            "sourceEntityToken": "tok-1",
            # B6: the daemon answers with the home it serves; the CLI exports
            # that path instead of resolving one of its own -- and stamps the
            # bundle with the daemon's own machine key (slice 2c).
            "sourceHome": str(agent_home),
            "signingHome": str(signing_home or agent_home.parent.parent),
        },
    }


def _agent_home_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prepare: Any = None,
) -> tuple[FakeLocal, FakeRemote, dict[str, Any]]:
    """The one place this module wires the P2 branch onto the fakes."""

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    # The chain is open for the harness a two-daemon run proved; a fake daemon
    # would answer the same either way, so the source spec has to say `pi` or
    # these tests would model a harness the real fence refuses (B8).
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    local.complete = {
        "ok": True,
        "removedAgent": True,
        "chain": {"action": "complete", "targetPhase": "active", "reason": None},
    }
    remote.receive = {
        "ok": True,
        "actor": f"agent:{OWNER}:{TARGET}:wk",
        "phases": ["landed", "active"],
        "sandboxSmoke": "NOT_RUN",
        "chain": {"report": {"operationId": "transfer-p2:op", "row": {"phase": "active"}}},
    }
    if prepare is not None:
        prepare(local, remote)
    return local, remote, _run(tmp_path, local, remote)


def test_agent_home_dry_run_reports_the_chain_and_touches_nothing(
    tmp_path: Path,
) -> None:
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)

    result = _run(tmp_path, local, remote, dry_run=True)

    assert result["ok"] is True
    assert result["dryRun"] is True
    assert result["mode"] == "agent-home-p2"
    # One digest over the plan's own facts, so both halves join on a value the
    # operator can recompute; the fence is named as the daemon's call.
    assert len(result["chain"]["planDigest"]) == 64
    assert result["fence"] == "daemon-decided-at-quiesce"
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    # `daemon status` fences the node; the precheck is the target's own name admission
    # (B2) -- a dry run validates both, and stops nothing.
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == ["daemon", "precheck"]
    assert remote.syncs == [] and remote.uploads == []
    assert remote.dirs == [] and remote.removed == []


def test_agent_home_dry_run_refuses_a_harness_the_fence_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: before ③-4 even ``--dry-run`` refused an agent-home worker.

    The refusal moved to the daemon, not away: a dry run that answered
    ``ok: true`` for a harness the same table refuses would print a plan the
    real call cannot run, and the old refusal would have no test left.

    The example's own row is removed in-process: production now proves every
    harness it names, so the arm pins the fence's refusal without depending on
    which rows the table happens to carry.
    """

    from hyprial.daemon.impl.transfer.p2.orchestration import lift as lift_module

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        tuple(row for row in lift_module.PROVEN_CHAINS if row.harness != "claude"),
    )
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "claude"  # unproven here: its own row is removed above
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)

    result = _run(tmp_path, local, remote, dry_run=True)

    assert result["ok"] is False
    assert result["dryRun"] is True and result["refused"] is True
    assert result["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert result["mode"] == "agent-home-p2"
    assert result["harness"] == "claude"
    assert "chain" not in result
    # Nothing was stopped, shipped or even asked of the daemon.
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == ["daemon", "precheck"]
    assert remote.syncs == [] and remote.removed == [] and remote.dirs == []


def test_agent_home_dry_run_refuses_codex_when_its_row_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run is one fence with three entries: delete the row and it refuses.

    The daemon entries are read through the frozen tree in the e2e apparatus;
    here the same table is emptied in-process (``chain_is_proven`` reads the
    ``lift`` module global), so the CLI dry-run entry is shown to speak the
    same refusal for a harness whose published row is gone.
    """

    from hyprial.daemon.impl.transfer.p2.orchestration import lift as lift_module

    monkeypatch.setattr(lift_module, "PROVEN_CHAINS", ())
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "codex"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)

    result = _run(tmp_path, local, remote, dry_run=True)

    assert result["ok"] is False
    assert result["dryRun"] is True and result["refused"] is True
    assert result["code"] == "TRANSFER_UNSUPPORTED_HARNESS"
    assert result["mode"] == "agent-home-p2"
    assert result["harness"] == "codex"
    assert "chain" not in result
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == ["daemon", "precheck"]
    assert remote.syncs == [] and remote.removed == [] and remote.dirs == []


def test_agent_home_worker_runs_the_p2_chain_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local, remote, result = _agent_home_run(tmp_path, monkeypatch)

    assert result["ok"] is True
    assert result["mode"] == "agent-home-p2"
    assert result["to"] == f"agent:{OWNER}:{TARGET}:wk"
    assert result["chain"]["targetPhase"] == "active"
    assert result["chain"]["sourceHold"] is None
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.complete",
    ]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == [
        "daemon",
        "precheck",
        # The target's credential-envelope key is fetched before the source is
        # stopped: a target that cannot name its half must not cost the worker
        # its hold, and nothing may ship credentials unsealed.
        "recipient-key",
        "receive",
    ]
    # The quiesce asked for the chain, and the receive brings the SAME facts to
    # the target: one fact set, two ledgers, no drift.
    quiesce_params = local.calls[1][1]
    payload = remote.receive_payload
    assert payload is not None
    assert payload["planDigest"] == quiesce_params["planDigest"]
    assert payload["sourceEntityToken"] == "tok-1"
    assert payload["sourceNode"] == SOURCE and payload["targetActor"] == "wk"
    assert payload["bundleDir"].startswith(str(Path("/remote/state")))
    # Two transfer units ship, bundle first then the worktree, both before
    # the receive call.  The bundle is the agent HOME, never the worktree
    # (D1: ``remote.syncs[0]`` must not be the cwd).
    assert [remote_dir for _local, remote_dir in remote.syncs] == [
        payload["bundleDir"],
        "/work/proj",
    ]
    assert remote.syncs[0][0] != Path("/work/proj")
    assert remote.syncs[1] == (Path("/work/proj"), "/work/proj")
    # The completion carries the target's own report, unchanged.
    assert local.calls[2][1]["chain"] == {"report": remote.receive["chain"]["report"]}


def test_a_target_that_cannot_name_its_key_leaves_the_source_running(
    tmp_path: Path,
) -> None:
    """No recipient key, no sealed envelope -- so nothing is stopped at all."""

    home = tmp_path / "hyprial-home"
    _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    remote.fail_on = ["agent", "transfer", "recipient-key"]

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_REMOTE_FAILED"
    assert "refusing to ship credentials unsealed" in str(caught.value)
    # The source is untouched: no quiesce, no hold, no rollback machinery.
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == [
        "daemon",
        "precheck",
        "recipient-key",
    ]


def test_the_source_adapter_pins_ride_the_receive_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: a worker's adapter pins move with it, exactly as P0 carries them.

    A re-pin is a MOVE by registry semantics, so the source does not decide it:
    the list travels in the same payload as the identity facts and the target
    asks its own gate.  Without it the pins would silently vanish when the
    source row retires.
    """

    def pin_source(local: FakeLocal, _remote: FakeRemote) -> None:
        local.quiesce["agent"] = {"pinnedAdapters": ["slack"]}

    _local, remote, result = _agent_home_run(tmp_path, monkeypatch, prepare=pin_source)

    assert result["ok"] is True
    assert remote.receive_payload is not None
    assert remote.receive_payload["pins"] == ["slack"]


def test_the_pin_list_is_the_snapshot_it_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list is read from the source's own snapshot, not invented here."""

    local, remote, _result = _agent_home_run(tmp_path, monkeypatch)

    assert remote.receive_payload is not None
    assert remote.receive_payload["pins"] == local.quiesce["agent"]["pinnedAdapters"]


@pytest.mark.parametrize("overrides", [
    {"containerized": True},
    {"container_image": "hyprial-worker:test"},
])
def test_container_flags_are_refused_not_silently_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    """B5: asking for container mode and getting a non-container transfer is
    a silent downgrade -- it answers a request nobody made."""

    home = tmp_path / "hyprial-home"
    _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, **overrides)

    assert caught.value.code == "TRANSFER_PREREQUISITE"
    assert "cannot land inside a container" in str(caught.value)
    assert caught.value.data["mode"] == "agent-home-p2"
    # The source is untouched and the target was not even asked.
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == ["daemon"]
    assert remote.syncs == [] and remote.dirs == []


def test_container_flags_are_refused_on_a_dry_run_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal is a fact about the request, not about the run."""

    home = tmp_path / "hyprial-home"
    _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote, dry_run=True, containerized=True)

    assert caught.value.code == "TRANSFER_PREREQUISITE"


def test_a_target_name_conflict_stops_before_the_source_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: P0's "conflicts stop before quiesce" invariant, on the P2 chain.

    The target's own admission is a read; learning it after the source has been
    fenced and its home shipped would make a name collision cost a transfer.
    """

    home = tmp_path / "hyprial-home"
    _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.spec["provider"] = "pi"
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    remote.precheck = {"ok": True, "conflicts": ['registered agent "wk"']}

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_CONFLICT"
    assert "registered agent" in str(caught.value)
    # The source was never fenced: no quiesce, no chain, nothing shipped.
    assert [method for method, _ in local.calls] == ["transfer.plan"]
    assert [argv[2] if argv[:2] == ["agent", "transfer"] else argv[0] for argv in remote.calls] == ["daemon", "precheck"]
    assert remote.syncs == [] and remote.dirs == []


def test_the_ship_creates_the_target_staging_directory_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: ``rsync -a`` does not create the path above its destination.

    Linux targets run GNU rsync, which fails outright there; macOS openrsync
    creates the parents and hid exactly this.  The order is the contract.
    """

    _local, remote, _result = _agent_home_run(tmp_path, monkeypatch)

    assert remote.dirs == ["/remote/state/transfer-incoming", "/work/proj"]
    assert remote.syncs, "the bundle still ships"
    assert remote.syncs[0][1].startswith("/remote/state/transfer-incoming/")


def test_the_worktree_ships_additively_onto_the_target_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: the working tree ships with the bundle, exactly as P0 migrates it.

    The bundle carries the agent HOME; a clean target has no cwd for the
    resume and pi's trust check additionally needs the ``.git`` boundary the
    source tree carries.  The worktree is additive (``sync_tree`` is
    ``rsync -a`` with no ``--delete``), so a non-empty target cwd is merged
    into, never cleared.
    """

    _local, remote, result = _agent_home_run(tmp_path, monkeypatch)

    assert result["ok"] is True
    # The source cwd is the spec's cwd, not the bundle staging directory.
    assert remote.syncs[1] == (Path("/work/proj"), "/work/proj")
    # Created before the ship, and exactly once: GNU rsync creates the final
    # component but not the path above it.
    assert remote.dirs == ["/remote/state/transfer-incoming", "/work/proj"]
    # Additive merge is the only worktree operation under test: the destination
    # is never removed (the bundle staging dir is the only thing ever removed).
    assert "/work/proj" not in remote.removed


def test_the_worktree_content_reaches_the_target_cwd_additively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 content identity + merge, at the chain's own seam.

    The design §4.1 asked for a two-daemon e2e marker assertion, but that e2e
    (``p2/e2e/test_two_daemon_chain.py``) drives ``transfer.quiesce`` /
    ``transfer.receive`` directly and never runs ``_run_agent_home_chain``,
    where D1's worktree ship lives -- an assertion there would not exercise
    the step at all.  The deviation is recorded in the evidence; the content
    routing is pinned here on the real chain instead, with an additive copy
    standing in for ``rsync -a``: the source marker arrives at the target cwd,
    and a pre-existing target file survives (merge, not clear).
    """

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    source_cwd = tmp_path / "source-work"
    source_cwd.mkdir()
    (source_cwd / "T3_MARKER.txt").write_text(
        "t3-worktree-marker\n", encoding="utf-8"
    )
    target_cwd = tmp_path / "target-work"
    target_cwd.mkdir()
    # A pre-existing target file the additive copy must preserve.
    (target_cwd / "TARGET_ONLY.txt").write_text("keep me\n", encoding="utf-8")

    local, remote = FakeLocal(), _CopyingRemote(source_cwd=source_cwd)
    local.spec["provider"] = "pi"
    local.spec["cwd"] = str(source_cwd)
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    local.complete = {
        "ok": True,
        "removedAgent": True,
        "chain": {"action": "complete", "targetPhase": "active", "reason": None},
    }
    remote.receive = {
        "ok": True,
        "actor": f"agent:{OWNER}:{TARGET}:wk",
        "phases": ["landed", "active"],
        "sandboxSmoke": "NOT_RUN",
        "chain": {
            "report": {"operationId": "transfer-p2:op", "row": {"phase": "active"}}
        },
    }

    result = _run(tmp_path, local, remote, target_cwd=str(target_cwd))

    assert result["ok"] is True
    assert (target_cwd / "T3_MARKER.txt").read_text(encoding="utf-8") == (
        "t3-worktree-marker\n"
    )
    # Additive merge: the target's own file is not deleted by the ship.
    assert (target_cwd / "TARGET_ONLY.txt").read_text(encoding="utf-8") == (
        "keep me\n"
    )
    # The receive carried the same destination cwd the content landed on.
    assert remote.receive_payload is not None
    assert remote.receive_payload["spec"]["cwd"] == str(target_cwd)


def test_a_failed_worktree_ship_rolls_back_before_the_receive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 negative arm: the worktree is a transfer unit, so its failure is a
    pre-receive failure -- the same rollback as a failed bundle ship, and the
    target is never asked to receive anything."""

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    remote.fail_ship_local = Path("/work/proj")

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_REMOTE_FAILED"
    assert caught.value.data["stage"] == "bundle/ship"
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.custody.override",
        "transfer.resume",
    ]
    assert all(argv[:3] != ["agent", "transfer", "receive"] for argv in remote.calls)
    # The bundle shipped first, then the worktree failed: the failing sync is
    # not recorded, the bundle is cleaned up, and the destination worktree is
    # never removed (nothing touches the target after a pre-receive failure).
    assert len(remote.syncs) == 1
    assert remote.syncs[0][1].startswith("/remote/state/transfer-incoming/")
    assert remote.removed == [remote.syncs[0][1]]
    assert "/work/proj" not in remote.removed


def test_a_received_bundle_is_removed_from_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: the exported home (which carries ``secrets/``) does not stay there."""

    _local, remote, result = _agent_home_run(tmp_path, monkeypatch)

    assert result["ok"] is True
    assert len(remote.removed) == 1
    assert remote.removed[0] == remote.syncs[0][1]


def test_the_quiesce_round_trip_outlasts_the_daemon_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: a legal slow stop is the daemon's verdict, never a client timeout.

    The budget is read from the declared contract rather than copied, so the
    two can only drift loudly.
    """

    bounds = json.loads(
        (REPO / "contract/daemon-lifecycle/p2-source-bounds.json").read_text()
    )
    local, _remote, _result = _agent_home_run(tmp_path, monkeypatch)

    assert [method for method, _ in local.calls][1] == "transfer.quiesce"
    timeout = local.options[1]["timeout"]
    assert timeout >= bounds["p2_quiesce_wait_seconds"]
    assert timeout >= bounds["source_quiesce_deadline_seconds"] + 30


def test_an_unknown_source_half_says_what_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: ``unknown`` is not "nothing happened", and the message must not lie."""

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, state="unknown", agent_home=agent_home)

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_CUSTODY_UNAVAILABLE"
    message = str(caught.value)
    assert "'unknown'" in message
    assert "the admission and the hold are recorded" in message
    assert "untouched" not in message
    assert caught.value.data["operationId"].startswith("transfer-p2:")
    # Nothing was shipped and nothing was rolled back: the hold is the daemon's
    # reported state, not something this half invents a compensating act for.
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
    ]
    assert remote.syncs == [] and remote.dirs == []


def test_the_capability_fence_speaks_from_the_daemon_unchanged(
    tmp_path: Path,
) -> None:
    """The CLI does not decide the fence a second time: the daemon's refusal
    reaches the operator with its own code and data, and nothing was stopped."""

    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    refusal = _Refusal(
        "TRANSFER_UNSUPPORTED_HARNESS",
        {"harness": "pi", "name": "wk", "mode": "agent-home-p2"},
    )
    local.raise_on["transfer.quiesce"] = refusal

    with pytest.raises(_Refusal) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_UNSUPPORTED_HARNESS"
    assert caught.value.data["mode"] == "agent-home-p2"
    # Nothing shipped, nothing rolled back: a refusal before the hold exists
    # must not leave a resume or an override in its wake.
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
    ]
    assert remote.syncs == []
    assert all(argv[:3] != ["agent", "transfer", "receive"] for argv in remote.calls)


def test_a_failed_ship_rolls_back_through_the_audited_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2, first arm: before the receive call the target cannot have been
    touched, so the source comes back and the attempt is clean."""

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    remote.fail_ship = True

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_REMOTE_FAILED"
    assert caught.value.data["stage"] == "bundle/ship"
    methods = [method for method, _ in local.calls]
    assert methods == [
        "transfer.plan",
        "transfer.quiesce",
        "transfer.custody.override",
        "transfer.resume",
    ]
    override = local.calls[2][1]
    assert override["principal"] == OWNER
    assert override["actor"] == "wk"
    assert override["operationId"].startswith("transfer-p2:")
    assert override["reason"].startswith("transfer rollback to")
    # The rollback result rides along, so the operator sees the worker is back
    # (or why it is not) instead of inferring it.
    assert caught.value.data["rollback"]["override"] == local.override
    assert caught.value.data["rollback"]["resume"] == local.resume
    assert caught.value.data["rollbackError"] is None
    # B4: with nothing left to release, the message may say the hold is gone.
    assert "the source hold was released through the audited override" in str(caught.value)
    # The partial remote directory went with it: nothing is left to read.
    assert len(remote.removed) == 1
    assert remote.removed[0].startswith("/remote/state/transfer-incoming/")


def test_a_rollback_that_did_not_complete_does_not_claim_it_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4: "the hold was released" is a claim about the world; a failed
    override makes it false, and the operator's next move is not the same."""

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    remote.fail_ship = True
    local.raise_on["transfer.custody.override"] = RuntimeError("override refused")

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    message = str(caught.value)
    assert "did NOT complete" in message
    assert "may still stand" in message
    assert "transfer.custody.override" in message
    assert "the source hold was released through the audited override" not in message
    assert caught.value.data["rollbackError"] is not None
    # The failure itself is unchanged: the ship is still what went wrong.
    assert caught.value.data["stage"] == "bundle/ship"


def test_an_unresolved_receive_keeps_the_hold_and_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2, second arm: after the call is issued the half holds.

    The target may already have landed this worker; releasing the source could
    leave two live workers, and deleting the bundle could break a landing that
    is still reading it.  Both facts are reported instead.
    """

    home = tmp_path / "hyprial-home"
    agent_home = _seed_agent_home(home)
    local, remote = FakeLocal(), FakeRemote()
    local.plan["agent"]["config"] = _agent_home_plan(tmp_path)
    _chain_quiesce(local, agent_home=agent_home)
    remote.fail_on = ["agent", "transfer", "receive"]

    with pytest.raises(TransferError) as caught:
        _run(tmp_path, local, remote)

    assert caught.value.code == "TRANSFER_REMOTE_FAILED"
    assert caught.value.data["stage"] == "receive-unresolved"
    assert caught.value.data["sourceHold"] == "kept"
    assert caught.value.data["bundleDir"] == remote.syncs[0][1]
    assert "unresolved half only holds" in str(caught.value)
    # No compensation: no override, no resume, and the bundle stays put.
    assert [method for method, _ in local.calls] == [
        "transfer.plan",
        "transfer.quiesce",
    ]
    assert remote.removed == []


def test_a_complete_that_holds_the_source_is_not_reported_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5: the target is live, so this is not a rollback -- but it is not success."""

    def hold_source(local: FakeLocal) -> None:
        local.complete = {
            "ok": True,
            "removedAgent": False,
            "chain": {
                "action": "hold",
                "targetPhase": "active",
                "reason": "source-liveness-unknown",
            },
        }

    local, _remote, result = _agent_home_run(tmp_path, monkeypatch, prepare=lambda left, _right: hold_source(left))

    assert result["ok"] is False
    assert result["error"].startswith("source half still held")
    assert result["chain"]["action"] == "hold"
    assert result["chain"]["sourceHold"] == "kept"
    # The target's own facts survive: the worker really is live over there.
    assert result["to"] == f"agent:{OWNER}:{TARGET}:wk"
    assert result["source"]["removedAgent"] is False


def test_the_chain_stamps_the_bundle_with_the_daemons_own_home_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 2c: the exporting machine's own key signs, and only it.

    The signature is what lets the target's gate attribute the bundle at all,
    so the chain has to stamp the sealed digest before the ship.  The key is
    the one the daemon *answered with* (``chain.signingHome``), and it lives in
    that home -- never inside the payload it vouches for, which is the whole
    reason a bundle-level key would be a defect.
    """

    home = tmp_path / "hyprial-home"

    seen: dict[str, Any] = {}
    real_sign = attribution_module.sign_exported_bundle

    def spy(bundle_dir, *, home, credential_envelope=None):
        bundle_dir = Path(bundle_dir)
        block = real_sign(
            bundle_dir, home=home, credential_envelope=credential_envelope
        )
        seen["bundle_dir"] = bundle_dir
        seen["signing_home"] = Path(home)
        seen["key_id"] = block.key_id
        seen["stamped"] = (bundle_dir / "signature.json").is_file()
        seen["in_payload"] = (
            bundle_dir / "payload" / "secrets" / "transfer" / "signing-key.json"
        ).exists()
        return block

    # The chain imports the signer inside its own body, so the patch goes on the
    # module that defines it.
    monkeypatch.setattr(attribution_module, "sign_exported_bundle", spy)

    _local, remote, result = _agent_home_run(tmp_path, monkeypatch)

    assert result["ok"] is True
    assert seen["signing_home"] == home
    assert seen["stamped"] is True
    assert seen["in_payload"] is False
    _private_bytes, public_bytes = read_signing_key(signing_key_path(home))
    assert seen["key_id"] == public_key_id(public_bytes)
    # The shipped tree is the stamped one: the same directory went to rsync,
    # and the stamp was in it before the ship.
    assert remote.syncs[0][0] == seen["bundle_dir"]
