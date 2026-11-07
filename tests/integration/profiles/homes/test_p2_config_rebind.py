"""Supported P2 config rebind recovery through the real daemon surface."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import ClassVar

import pytest

import hyprial.daemon.impl.harnesses.runtime.launcher as launcher_module
import hyprial.identity.impl.agents.config_rebind.service as rebind_service
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.identity import (
    AgentConfig,
    agent_config_digest,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)


class _StubPiProcess:
    """Only the final process boundary is replaced; daemon launch wiring is real."""

    instances: ClassVar[list["_StubPiProcess"]] = []

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        worker_channel: object,
        runtime_launch_custody: object,
        **_kwargs: object,
    ) -> None:
        self.spec = spec
        self.worker_channel = worker_channel
        self.running = True
        self.pid = None
        context = worker_channel.runtime_context  # type: ignore[attr-defined]
        assert context is not None
        self._custody = runtime_launch_custody(context)  # type: ignore[operator]
        self._custody.__enter__()
        self.instances.append(self)

    @property
    def in_flight(self) -> int:
        return 0

    @property
    def queue_depth(self) -> int:
        return 0

    def stop(self) -> None:
        if self.running:
            self.running = False
            self._custody.__exit__(None, None, None)


def _new_application(root: Path) -> DaemonApplication:
    root.mkdir(mode=0o700, exist_ok=True)
    home = root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    state = root / "state"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        listen = f"tcp/127.0.0.1:{reservation.getsockname()[1]}"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="node-1",
        owner="owner",
        hyprial_home=home,
        zenoh_listen=(listen,),
        zenoh_connect=(),
        **_deep_module_daemon_dependencies(),
    )
    app.network_isolated = True
    app._start_runtime()
    return app


def _close_application(app: DaemonApplication) -> None:
    try:
        app._close()
    finally:
        app._close_home_lease()


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    _StubPiProcess.instances = []
    monkeypatch.setattr(launcher_module, "PiRpcProcess", _StubPiProcess)
    app = _new_application(tmp_path)
    try:
        yield app
    finally:
        _close_application(app)


def _private_tree(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(mode=0o700)
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        for parent in (target.parent, *target.parent.parents):
            if parent == root.parent:
                break
            parent.chmod(0o700)
        target.write_bytes(body)
        target.chmod(0o600)
    return root


def _routine_projection(app: DaemonApplication, actor: str) -> tuple[object, ...]:
    assert app._routine_service is not None
    rows = app._routine_service.list()["routines"]
    return tuple(
        (row.get("name"), row.get("enabled"), row.get("definition"), row.get("version"))
        for row in rows
        if str(row.get("actor", "")).endswith(f":{actor}")
    )


def test_rebind_repairs_malformed_c_real_headless_start_and_exact_rollback(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "manager", "preferredHarness": "pi"})
    receipt_before = app._agent_registry.home_receipt("manager")
    home = Path(receipt_before.path)
    malformed = home / "config" / "AGENTS.md"
    malformed.write_bytes(b"# malformed root persona\n")
    malformed.chmod(0o600)
    misplaced_routine = home / "config" / "nas-manager-routine.yaml"
    misplaced_routine.write_bytes(b"version: 2\nmode: scheduled\n")
    misplaced_routine.chmod(0o600)
    routine_before = _routine_projection(app, "manager")

    app._agent_session_domains.agent.prepare_workspace("manager")
    start = {
        "provider": "pi",
        "name": "manager",
        "headless": True,
        "cwd": str(home / "workspace"),
    }
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("lifecycle.start", start)
    assert failed.value.code == "AGENT_HOME_ACCESS_FAILED"
    agent_before = app.agents.require("manager")

    source = _private_tree(
        tmp_path / "staging",
        {"native/pi/AGENTS.md": b"# repaired persona\n"},
    )
    dry = app.handle(
        "agent.config.rebind",
        {"name": "manager", "source": str(source), "dryRun": True},
    )
    assert dry["dryRun"] is True
    assert dry["receiptId"] is None
    assert dry["receipt"]["receiptId"] is None
    assert malformed.read_bytes() == b"# malformed root persona\n"
    assert app.agents.require("manager") == agent_before

    rebound = app.handle(
        "agent.config.rebind", {"name": "manager", "source": str(source)}
    )
    assert rebound["state"] == "complete"
    assert rebound["beforeHash"] != rebound["afterHash"]
    assert misplaced_routine.read_bytes() == b"version: 2\nmode: scheduled\n"
    assert not Path(str(rebound["target"]), "nas-manager-routine.yaml").exists()
    assert _routine_projection(app, "manager") == routine_before
    current = app.agents.require("manager")
    assert (current.uri, current.entity_token) == (
        agent_before.uri,
        agent_before.entity_token,
    )
    assert app._agent_registry.home_receipt("manager") == receipt_before

    started = app.handle("lifecycle.start", start)
    assert started["ok"] is True
    assert len(_StubPiProcess.instances) == 1
    context = _StubPiProcess.instances[0].worker_channel.runtime_context
    assert context.roots.projection_root.joinpath("AGENTS.md").read_bytes() == (
        b"# repaired persona\n"
    )
    with pytest.raises(DaemonRequestError) as running:
        app.handle(
            "agent.config.rebind",
            {"name": "manager", "rollbackReceiptId": rebound["receiptId"]},
        )
    assert running.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert running.value.data["runtime"] is True
    app.handle("down", {"provider": "pi", "target": "manager"})

    rolled = app.handle(
        "agent.config.rebind",
        {"name": "manager", "rollbackReceiptId": rebound["receiptId"]},
    )
    assert rolled["state"] == "rolled-back"
    assert malformed.read_bytes() == b"# malformed root persona\n"
    assert stat.S_IMODE(malformed.stat().st_mode) == 0o600
    assert misplaced_routine.read_bytes() == b"version: 2\nmode: scheduled\n"
    assert stat.S_IMODE(misplaced_routine.stat().st_mode) == 0o600
    rolled_agent = app.agents.require("manager")
    assert (rolled_agent.uri, rolled_agent.entity_token, rolled_agent.config) == (
        agent_before.uri,
        agent_before.entity_token,
        agent_before.config,
    )
    assert app._agent_registry.home_receipt("manager") == receipt_before
    assert _routine_projection(app, "manager") == routine_before


@pytest.mark.parametrize(
    "crash_label",
    [
        "receipt-prepared",
        "tree-published",
        "publish-recorded",
        "binding-updated",
        "complete-recorded",
    ],
)
def test_rebind_resumes_every_forward_crash_boundary(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_label: str,
) -> None:
    app = application
    app.handle("agent.create", {"name": "resumable", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "resume-staging", {"native/pi/AGENTS.md": b"resumed\n"}
    )
    crashed = False

    def crash_once(label: str) -> None:
        nonlocal crashed
        if label == crash_label and not crashed:
            crashed = True
            raise RuntimeError(f"simulated crash at {label}")

    monkeypatch.setattr(rebind_service, "_crash_point", crash_once)
    with pytest.raises(RuntimeError, match="simulated crash"):
        app.handle("agent.config.rebind", {"name": "resumable", "source": str(source)})
    assert crashed is True
    resumed = app.handle(
        "agent.config.rebind", {"name": "resumable", "source": str(source)}
    )
    assert resumed["state"] == "complete"
    assert (
        Path(str(resumed["target"]), "native/pi/AGENTS.md").read_bytes() == b"resumed\n"
    )


def test_rebind_resumes_after_daemon_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "restart-daemon"
    source = _private_tree(
        tmp_path / "restart-staging", {"native/pi/AGENTS.md": b"resumed\n"}
    )
    first = _new_application(root)
    try:
        first.handle("agent.create", {"name": "restarted", "preferredHarness": "pi"})
        crashed = False

        def crash_once(label: str) -> None:
            nonlocal crashed
            if label == "publish-recorded" and not crashed:
                crashed = True
                raise RuntimeError("simulated daemon loss")

        monkeypatch.setattr(rebind_service, "_crash_point", crash_once)
        with pytest.raises(RuntimeError, match="simulated daemon loss"):
            first.handle(
                "agent.config.rebind", {"name": "restarted", "source": str(source)}
            )
        assert crashed is True
    finally:
        _close_application(first)

    monkeypatch.setattr(rebind_service, "_crash_point", lambda _label: None)
    second = _new_application(root)
    try:
        resumed = second.handle(
            "agent.config.rebind", {"name": "restarted", "source": str(source)}
        )
        assert resumed["state"] == "complete"
        assert Path(str(resumed["target"]), "native/pi/AGENTS.md").read_bytes() == (
            b"resumed\n"
        )
    finally:
        _close_application(second)


def test_prepared_rebind_rolls_back_after_daemon_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "prepared-rollback-daemon"
    source = _private_tree(
        tmp_path / "prepared-rollback-staging",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    first = _new_application(root)
    try:
        first.handle(
            "agent.create", {"name": "prepared-restart", "preferredHarness": "pi"}
        )
        before = first.agents.require("prepared-restart").config
        home = Path(first._agent_registry.home_receipt("prepared-restart").path)

        def crash_after_receipt(label: str) -> None:
            if label == "receipt-prepared":
                raise RuntimeError("simulated loss after prepared receipt")

        monkeypatch.setattr(rebind_service, "_crash_point", crash_after_receipt)
        with pytest.raises(RuntimeError, match="simulated loss after prepared receipt"):
            first.handle(
                "agent.config.rebind",
                {"name": "prepared-restart", "source": str(source)},
            )
        transactions = list((home / "state/config-rebind").iterdir())
        assert len(transactions) == 1
        transaction = transactions[0]
        receipt_id = transaction.name
        assert (transaction / "new/native/pi/AGENTS.md").is_file()
    finally:
        _close_application(first)

    monkeypatch.setattr(rebind_service, "_crash_point", lambda _label: None)
    second = _new_application(root)
    try:
        rolled = second.handle(
            "agent.config.rebind",
            {"name": "prepared-restart", "rollbackReceiptId": receipt_id},
        )
        assert rolled["state"] == "rolled-back"
        assert second.agents.require("prepared-restart").config == before
        assert not (transaction / "new").exists()
        assert not (transaction / "retired-target").exists()
        assert not Path(str(rolled["target"])).exists()
    finally:
        _close_application(second)


def test_prepared_rollback_resumes_interrupted_staged_cleanup(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "prepared-cleanup", "preferredHarness": "pi"})
    home = Path(app._agent_registry.home_receipt("prepared-cleanup").path)
    source = _private_tree(
        tmp_path / "prepared-cleanup-staging",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )

    def crash_after_receipt(label: str) -> None:
        if label == "receipt-prepared":
            raise RuntimeError("simulated loss after prepared receipt")

    monkeypatch.setattr(rebind_service, "_crash_point", crash_after_receipt)
    with pytest.raises(RuntimeError, match="simulated loss after prepared receipt"):
        app.handle(
            "agent.config.rebind",
            {"name": "prepared-cleanup", "source": str(source)},
        )
    transaction = next((home / "state/config-rebind").iterdir())
    receipt_id = transaction.name
    original_remove = rebind_service.remove_tree
    interrupted = False

    def interrupt_cleanup(path: Path) -> None:
        nonlocal interrupted
        if path.name == "retired-target" and not interrupted:
            interrupted = True
            path.joinpath("native/pi/AGENTS.md").unlink()
            raise RuntimeError("simulated loss during staged-tree cleanup")
        original_remove(path)

    monkeypatch.setattr(rebind_service, "_crash_point", lambda _label: None)
    monkeypatch.setattr(rebind_service, "remove_tree", interrupt_cleanup)
    rollback = {
        "name": "prepared-cleanup",
        "rollbackReceiptId": receipt_id,
    }
    with pytest.raises(RuntimeError, match="simulated loss during staged-tree cleanup"):
        app.handle("agent.config.rebind", rollback)
    assert interrupted is True
    interrupted_receipt = json.loads((transaction / "receipt.json").read_text())
    assert interrupted_receipt["phase"] == "binding-restored"
    assert not (transaction / "new").exists()
    assert (transaction / "retired-target").is_dir()

    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert not (transaction / "new").exists()
    assert not (transaction / "retired-target").exists()


@pytest.mark.parametrize(
    "crash_label", ["rollback-recorded", "binding-restored", "rollback-complete"]
)
def test_rebind_resumes_every_rollback_crash_boundary(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_label: str,
) -> None:
    app = application
    app.handle("agent.create", {"name": "rollback", "preferredHarness": "pi"})
    before = app.agents.require("rollback")
    source = _private_tree(
        tmp_path / "rollback-staging", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "rollback", "source": str(source)}
    )
    crashed = False

    def crash_once(label: str) -> None:
        nonlocal crashed
        if label == crash_label and not crashed:
            crashed = True
            raise RuntimeError(f"simulated crash at {label}")

    monkeypatch.setattr(rebind_service, "_crash_point", crash_once)
    rollback = {
        "name": "rollback",
        "rollbackReceiptId": rebound["receiptId"],
    }
    with pytest.raises(RuntimeError, match="simulated crash"):
        app.handle("agent.config.rebind", rollback)
    assert crashed is True
    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert app.agents.require("rollback").config == before.config
    assert not Path(str(rebound["target"])).exists()


def test_rollback_resumes_partial_retired_tree_cleanup(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "cleanup", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "cleanup-staging", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "cleanup", "source": str(source)}
    )
    original_remove = rebind_service.remove_tree
    interrupted = False

    def interrupt_cleanup(path: Path) -> None:
        nonlocal interrupted
        if path.name == "retired-target" and not interrupted:
            interrupted = True
            path.joinpath("native/pi/AGENTS.md").unlink()
            raise RuntimeError("simulated loss during retired-tree cleanup")
        original_remove(path)

    monkeypatch.setattr(rebind_service, "remove_tree", interrupt_cleanup)
    rollback = {"name": "cleanup", "rollbackReceiptId": rebound["receiptId"]}
    with pytest.raises(
        RuntimeError, match="simulated loss during retired-tree cleanup"
    ):
        app.handle("agent.config.rebind", rollback)
    assert interrupted is True
    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert not Path(str(rebound["target"])).exists()


def test_rebind_can_publish_multiple_revisions(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "repeat", "preferredHarness": "pi"})
    first_source = _private_tree(
        tmp_path / "first", {"native/pi/AGENTS.md": b"first\n"}
    )
    second_source = _private_tree(
        tmp_path / "second", {"native/pi/AGENTS.md": b"second\n"}
    )
    first = app.handle(
        "agent.config.rebind", {"name": "repeat", "source": str(first_source)}
    )
    second = app.handle(
        "agent.config.rebind", {"name": "repeat", "source": str(second_source)}
    )
    assert first["receiptId"] != second["receiptId"]
    assert Path(str(first["target"]), "native/pi/AGENTS.md").read_bytes() == b"first\n"
    assert (
        Path(str(second["target"]), "native/pi/AGENTS.md").read_bytes() == b"second\n"
    )
    assert app.agents.require("repeat").config == AgentConfig(str(second["target"]))


def test_rebind_refuses_linked_receipt_root(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "linked-receipt", "preferredHarness": "pi"})
    home = Path(app._agent_registry.home_receipt("linked-receipt").path)
    outside = tmp_path / "outside-receipts"
    outside.mkdir(mode=0o700)
    (home / "state" / "config-rebind").symlink_to(outside, target_is_directory=True)
    source = _private_tree(
        tmp_path / "linked-receipt-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )

    with pytest.raises(DaemonRequestError) as failed:
        app.handle(
            "agent.config.rebind", {"name": "linked-receipt", "source": str(source)}
        )
    assert failed.value.code == "AGENT_CONFIG_REBIND_RECEIPT_INVALID"
    assert list(outside.iterdir()) == []


def test_rebind_refuses_linked_revision_parent(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "linked-parent", "preferredHarness": "pi"})
    home = Path(app._agent_registry.home_receipt("linked-parent").path)
    outside = tmp_path / "outside-revisions"
    outside.mkdir(mode=0o700)
    (home / "config-revisions").symlink_to(outside, target_is_directory=True)
    source = _private_tree(
        tmp_path / "linked-parent-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )

    with pytest.raises(DaemonRequestError) as failed:
        app.handle(
            "agent.config.rebind", {"name": "linked-parent", "source": str(source)}
        )
    assert failed.value.code == "AGENT_CONFIG_REBIND_TARGET_DRIFT"
    assert list(outside.iterdir()) == []


def test_rebind_refuses_config_binding_cas_conflict(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "cas", "preferredHarness": "pi"})
    agent = app.agents.require("cas")
    source = _private_tree(
        tmp_path / "cas-staging", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    crashed = False

    def crash_once(label: str) -> None:
        nonlocal crashed
        if label == "receipt-prepared" and not crashed:
            crashed = True
            raise RuntimeError("simulated receipt crash")

    monkeypatch.setattr(rebind_service, "_crash_point", crash_once)
    with pytest.raises(RuntimeError, match="simulated receipt crash"):
        app.handle("agent.config.rebind", {"name": "cas", "source": str(source)})
    alternate_root = _private_tree(
        tmp_path / "alternate", {"native/pi/AGENTS.md": b"concurrent admin\n"}
    )
    alternate = AgentConfig(str(alternate_root))
    app._agent_registry.bind_agent_config(
        "cas",
        expected_entity_token=agent.entity_token,
        config=alternate,
        expected_current_config_digest=agent_config_digest(agent.config),
    )
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "cas", "source": str(source)})
    assert failed.value.code == "AGENT_CONFIG_CAS_MISMATCH"
    assert app._agent_registry.require("cas").config == alternate


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("entityToken", "stale-incarnation", "AGENT_INCARNATION_MISMATCH"),
        ("target", "/tmp/outside-agent-home", "AGENT_CONFIG_REBIND_RECEIPT_INVALID"),
        ("beforeConfigDigest", "0" * 64, "AGENT_CONFIG_REBIND_RECEIPT_INVALID"),
    ],
)
def test_rebind_refuses_stale_or_corrupt_receipt(
    application: DaemonApplication,
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    app = application
    app.handle("agent.create", {"name": "stale", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "stale-staging", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "stale", "source": str(source)}
    )
    receipt_path = (
        Path(app._agent_registry.home_receipt("stale").path)
        / "state/config-rebind"
        / str(rebound["receiptId"])
        / "receipt.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(DaemonRequestError) as failed:
        app.handle(
            "agent.config.rebind",
            {"name": "stale", "rollbackReceiptId": rebound["receiptId"]},
        )
    assert failed.value.code == code


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("source", "AGENT_CONFIG_REBIND_SOURCE_DRIFT"),
        ("target", "AGENT_CONFIG_REBIND_TARGET_DRIFT"),
    ],
)
def test_rollback_refuses_tree_drift(
    application: DaemonApplication,
    tmp_path: Path,
    drift: str,
    code: str,
) -> None:
    app = application
    app.handle("agent.create", {"name": f"{drift}-drift", "preferredHarness": "pi"})
    agent = app.agents.require(f"{drift}-drift")
    assert agent.config is not None
    source = _private_tree(
        tmp_path / f"{drift}-drift-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    rebound = app.handle(
        "agent.config.rebind",
        {"name": f"{drift}-drift", "source": str(source)},
    )
    if drift == "source":
        changed = Path(agent.config.source) / "late-file.md"
        changed.write_bytes(b"drift\n")
        changed.chmod(0o600)
    else:
        changed = Path(str(rebound["target"])) / "native/pi/AGENTS.md"
        changed.write_bytes(b"drift\n")
        changed.chmod(0o600)

    with pytest.raises(DaemonRequestError) as failed:
        app.handle(
            "agent.config.rebind",
            {
                "name": f"{drift}-drift",
                "rollbackReceiptId": rebound["receiptId"],
            },
        )
    assert failed.value.code == code


def test_rebind_invalidates_a_prepared_runtime_context(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "offered", "preferredHarness": "pi"})
    home = Path(app._agent_registry.home_receipt("offered").path)
    context = app._agent_session_domains.agent.prepare_runtime_context(
        "offered",
        "pi",
        str(home / "workspace"),
    )
    assert context is not None
    assert context.launch_token is not None
    source = _private_tree(
        tmp_path / "offered-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )

    rebound = app.handle(
        "agent.config.rebind", {"name": "offered", "source": str(source)}
    )
    assert rebound["state"] == "complete"
    failure: DaemonRequestError | None = None
    acquired: dict[str, object] | None = None
    try:
        acquired = app.handle(
            "agent.runtime-launch.acquire",
            {"launchToken": context.launch_token},
        )
    except DaemonRequestError as error:
        failure = error
    finally:
        if acquired is not None:
            app.handle(
                "agent.runtime-launch.release",
                {
                    "leaseToken": acquired["leaseToken"],
                    "operationId": acquired["operationId"],
                },
            )
    assert failure is not None
    assert failure.code == "AGENT_RUNTIME_CONTEXT_STALE"


def test_rebind_rechecks_durable_desired_before_binding_mutations(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "racing", "preferredHarness": "pi"})
    before = app.agents.require("racing").config
    source = _private_tree(
        tmp_path / "racing-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    spec = HarnessLaunchSpec(
        harness="pi", name="racing", headless=True, cwd=str(tmp_path), args=()
    )
    inserted = False

    def introduce_desired(label: str) -> None:
        nonlocal inserted
        if label == "publish-recorded" and not inserted:
            inserted = True
            app.desired_state.upsert_harness(spec)

    monkeypatch.setattr(rebind_service, "_crash_point", introduce_desired)
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "racing", "source": str(source)})
    assert failed.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert app.agents.require("racing").config == before

    app.desired_state.remove_harness("pi", "racing")
    resumed = app.handle(
        "agent.config.rebind", {"name": "racing", "source": str(source)}
    )
    assert resumed["state"] == "complete"


def test_rollback_rechecks_durable_desired_before_binding_mutations(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "rollback-race", "preferredHarness": "pi"})
    before = app.agents.require("rollback-race").config
    source = _private_tree(
        tmp_path / "rollback-race-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "rollback-race", "source": str(source)}
    )
    after = app.agents.require("rollback-race").config
    spec = HarnessLaunchSpec(
        harness="pi",
        name="rollback-race",
        headless=True,
        cwd=str(tmp_path),
        args=(),
    )
    inserted = False

    def introduce_desired(label: str) -> None:
        nonlocal inserted
        if label == "rollback-recorded" and not inserted:
            inserted = True
            app.desired_state.upsert_harness(spec)

    monkeypatch.setattr(rebind_service, "_crash_point", introduce_desired)
    rollback = {
        "name": "rollback-race",
        "rollbackReceiptId": rebound["receiptId"],
    }
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", rollback)
    assert failed.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert app.agents.require("rollback-race").config == after

    app.desired_state.remove_harness("pi", "rollback-race")
    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert app.agents.require("rollback-race").config == before


def test_rollback_resumes_after_atomic_target_retirement(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "retired-target", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "retired-target-source",
        {"native/pi/AGENTS.md": b"replacement\n"},
    )
    rebound = app.handle(
        "agent.config.rebind", {"name": "retired-target", "source": str(source)}
    )
    crashed = False

    def crash_after_retire(label: str) -> None:
        nonlocal crashed
        if label == "target-retired" and not crashed:
            crashed = True
            raise RuntimeError("simulated loss after target retirement")

    monkeypatch.setattr(rebind_service, "_crash_point", crash_after_retire)
    rollback = {
        "name": "retired-target",
        "rollbackReceiptId": rebound["receiptId"],
    }
    with pytest.raises(RuntimeError, match="simulated loss after target retirement"):
        app.handle("agent.config.rebind", rollback)
    assert crashed is True
    resumed = app.handle("agent.config.rebind", rollback)
    assert resumed["state"] == "rolled-back"
    assert not Path(str(rebound["target"])).exists()


def test_rebind_refuses_destroy_reservation(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "destroying", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "destroy-source", {"native/pi/AGENTS.md": b"replacement\n"}
    )
    reservation = app._reserve_agent_destroy("destroying")
    try:
        with pytest.raises(DaemonRequestError) as failed:
            app.handle(
                "agent.config.rebind",
                {"name": "destroying", "source": str(source)},
            )
        assert failed.value.code == "AGENT_MIGRATION_BUSY"
    finally:
        app._release_agent_destroy_reservation(reservation.reservation_token)


def test_rebind_refuses_worker_origin(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "operator-only", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "worker-source", {"native/pi/AGENTS.md": b"blocked\n"}
    )
    with pytest.raises(DaemonRequestError) as failed:
        app.handle(
            "agent.config.rebind",
            {"name": "operator-only", "source": str(source)},
            _trusted_message_origin="worker",
        )
    assert failed.value.code == "CALLER_NOT_AUTHORIZED"


def test_rebind_refuses_durable_desired_even_without_running_supervisor(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "desired", "preferredHarness": "pi"})
    source = _private_tree(tmp_path / "staging", {"native/pi/AGENTS.md": b"ok\n"})
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness="pi",
            name="desired",
            headless=True,
            cwd=str(tmp_path),
            args=(),
        )
    )
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "desired", "source": str(source)})
    assert failed.value.code == "AGENT_CONFIG_REBIND_NOT_QUIESCENT"
    assert failed.value.data["desired"] is True


@pytest.mark.parametrize(
    ("relative", "body", "code"),
    [
        ("AGENTS.md", b"root file\n", "AGENT_CONFIG_REBIND_INVALID_MAPPING"),
        (
            "native/unknown/AGENTS.md",
            b"unsupported harness\n",
            "AGENT_CONFIG_REBIND_INVALID_MAPPING",
        ),
        ("native/pi/auth.json", b"{}\n", "AGENT_CONFIG_REBIND_INVALID_MAPPING"),
        (
            "native/pi/settings.json",
            b"api_key = 'sk-secret'\n",
            "AGENT_CONFIG_REBIND_SECRET_CONTENT",
        ),
        (
            "native/pi/AGENTS.md",
            b"api_key must come from the environment\n"
            + b"x" * 128
            + b"\napi_key = 'sk-secret'\n",
            "AGENT_CONFIG_REBIND_SECRET_CONTENT",
        ),
        (
            "native/pi/AGENTS.md",
            b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
            "AGENT_CONFIG_REBIND_SECRET_CONTENT",
        ),
    ],
)
def test_rebind_refuses_invalid_mapping_and_secret_content(
    application: DaemonApplication,
    tmp_path: Path,
    relative: str,
    body: bytes,
    code: str,
) -> None:
    app = application
    app.handle("agent.create", {"name": "invalid", "preferredHarness": "pi"})
    source = _private_tree(tmp_path / relative.replace("/", "-"), {relative: body})
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "invalid", "source": str(source)})
    assert failed.value.code == code


def test_rebind_refuses_staging_owned_by_another_account(
    application: DaemonApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = application
    app.handle("agent.create", {"name": "foreign", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "foreign-source", {"native/pi/AGENTS.md": b"safe\n"}
    )
    original_lstat = Path.lstat

    def foreign_lstat(path: Path):  # type: ignore[no-untyped-def]
        info = original_lstat(path)
        if path == source or source in path.parents:
            fields = list(info)
            fields[4] = info.st_uid + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_lstat)
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "foreign", "source": str(source)})
    assert failed.value.code == "AGENT_CONFIG_REBIND_OWNERSHIP"


def test_rebind_refuses_non_utf8_content(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "binary", "preferredHarness": "pi"})
    source = _private_tree(
        tmp_path / "binary-source", {"native/pi/AGENTS.md": b"invalid: \xff\n"}
    )
    with pytest.raises(DaemonRequestError) as failed:
        app.handle("agent.config.rebind", {"name": "binary", "source": str(source)})
    assert failed.value.code == "AGENT_CONFIG_REBIND_INVALID_SOURCE"


def test_rebind_refuses_symlink_and_unsafe_mode(
    application: DaemonApplication, tmp_path: Path
) -> None:
    app = application
    app.handle("agent.create", {"name": "linked", "preferredHarness": "pi"})
    source = _private_tree(tmp_path / "linked", {"native/pi/real.md": b"ok\n"})
    (source / "native" / "pi" / "alias.md").symlink_to("real.md")
    with pytest.raises(DaemonRequestError) as linked:
        app.handle("agent.config.rebind", {"name": "linked", "source": str(source)})
    assert linked.value.code == "AGENT_CONFIG_REBIND_SYMLINK"
    (source / "native" / "pi" / "alias.md").unlink()
    (source / "native" / "pi" / "real.md").chmod(0o644)
    with pytest.raises(DaemonRequestError) as mode:
        app.handle("agent.config.rebind", {"name": "linked", "source": str(source)})
    assert mode.value.code == "AGENT_CONFIG_REBIND_MODE"
