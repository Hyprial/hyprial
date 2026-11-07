"""agent_home_dirs: layout scenarios."""

from __future__ import annotations
import json
import threading
from pathlib import Path
import pytest
from typer.testing import CliRunner
from hyprial.identity.impl.agents import (
    Agent,
    AgentExistsError,
    AgentHomeError,
    AgentNotFoundError,
    AgentRegistry,
)
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.home.effects import (
    AgentHomeEffect,
    AgentHomeEffectLane,
    CleanupHome,
)
from hyprial.identity.impl.agents.actor.ports import (
    CreateAgentCommand,
)
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.owner_migration import (
    migrate_owner,
)

from .support import (
    _isolated_environment as _isolated_environment,
    _registry,
    _track,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.support as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_create_replays_the_durable_home_claim_after_process_loss(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    pending = Agent(
        uri=registry.uri_for("worker"),
        actor="worker",
        owner="owner",
        machine="node",
        created_at_ms=registry.now_ms(),
    )
    claim = provisioner.claim_receipt(
        actor=pending.actor, entity_token=pending.entity_token
    )
    with registry._lock, registry._db:
        registry._record_home_resource_locked(claim, False)
        registry._record_external_resource_locked(
            "agent-record:worker", False, pending.to_json()
        )
    provisioner.provision_claimed(claim)

    resumed = registry.create("worker", model="later-request-must-not-replace")
    assert resumed.entity_token == pending.entity_token
    assert resumed.model is None
    assert registry.home_receipt("worker") == claim


def test_home_effect_lane_bounds_admission_and_retries_completion() -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    receipt = HomeReceipt(
        "worker", "entity", "resource", "/isolated/worker", 0,
        status="revoked",
    )

    class BlockingProvisioner:
        @staticmethod
        def cleanup(_receipt: HomeReceipt, *, expected_token: str) -> HomeReceipt:
            assert expected_token == "resource"
            entered.set()
            assert release.wait(3.0)
            return HomeReceipt(
                "worker", "entity", "cleaned", "/isolated/worker", 0,
                status="cleaned",
            )

    lane_box: list[AgentHomeEffectLane] = []
    attempts = 0

    def finish(event: object) -> AdmissionResult:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return AdmissionResult.OVERLOADED
        lane_box[0].acknowledge(
            getattr(event, "correlation_id"), getattr(event, "generation")
        )
        completed.set()
        return AdmissionResult.ACCEPTED

    lane = AgentHomeEffectLane(  # type: ignore[arg-type]
        BlockingProvisioner(), complete=finish, capacity=1, retry_seconds=0.001
    )
    lane_box.append(lane)
    try:
        first = AgentHomeEffect(
            "first-home-effect",
            1,
            CleanupHome(receipt),
        )
        second = AgentHomeEffect(
            "second-home-effect",
            1,
            CleanupHome(receipt),
        )
        assert lane.reserve(first.correlation_id, first.generation) is AdmissionResult.ACCEPTED
        assert lane.submit_reserved(first) is AdmissionResult.ACCEPTED
        assert entered.wait(1.0)
        assert lane.reserve(second.correlation_id, second.generation) is AdmissionResult.OVERLOADED
        release.set()
        assert completed.wait(1.0)
        assert attempts >= 3
    finally:
        release.set()
        assert lane.close(3.0)


def test_create_completion_cannot_overwrite_a_replaced_home_claim(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    reservation = registry.prepare_create_command(
        CreateAgentCommand("create-fence", "worker")
    )
    assert reservation.plan is not None
    completed = registry._execute_home_plan(reservation.plan)
    forged = registry.home_provisioner.claim_receipt(
        actor="worker", entity_token="new-incarnation"
    )
    with registry._lock, registry._db:
        registry._record_home_resource_locked(forged, False)

    with pytest.raises(AgentHomeError) as fenced:
        registry.commit_create_record(reservation, completed)
    assert fenced.value.category == "create-fenced"
    assert registry.get("worker") is None


def test_t01_home_key_survives_alias_harness_and_cwd_but_not_foreign_uri(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, owner="old-owner")
    agent = registry.create("Alpha", cwd="/first", preferred_harness="pi")
    receipt = registry.home_receipt("Alpha")
    registry.record_session("Alpha", harness="claude", session_id="session-1")
    registry.update("Alpha", cwd="/second")
    assert registry.home_receipt("Alpha") == receipt
    assert registry.require("Alpha").entity_token == agent.entity_token
    registry.close()

    state = tmp_path / "state"
    assert migrate_owner(
        state_dir=state,
        hyprial_home=tmp_path / "home",
        old="old-owner",
        new="alias-owner",
    ) > 0
    reopened = _registry(tmp_path, owner="alias-owner")
    assert reopened.home_receipt("Alpha") == receipt
    assert reopened.require("Alpha").entity_token == agent.entity_token
    foreign = "agent:other-owner:other-node:Alpha"
    assert reopened.local_actor(foreign) is None
    with pytest.raises(AgentNotFoundError):
        reopened.home_receipt(foreign)


def test_t02_provisioned_layout_has_private_modes_and_registry_receipt(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    agent = registry.create("worker")
    receipt = registry.home_receipt(agent.uri)
    root = tmp_path / "home" / "agents" / "worker"

    assert Path(receipt.path) == root
    assert receipt.actor == "worker"
    assert receipt.entity_token == agent.entity_token
    assert not (root / "secrets").exists()
    for path in (root, root / "config", root / "state"):
        assert path.is_dir()
        assert path.stat().st_mode & 0o777 == 0o700
    mirror = root / "state" / "home-receipt.json"
    assert mirror.stat().st_mode & 0o777 == 0o600
    assert json.loads(mirror.read_text())["resourceToken"] == receipt.resource_token


def test_workspace_is_optional_until_default_start_and_manual_0700_is_valid(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    registry.create("worker")
    root = Path(registry.home_receipt("worker").path)
    workspace = root / "workspace"

    assert not workspace.exists()
    workspace.mkdir(mode=0o700)
    assert registry.home_receipt("worker").path == str(root)
    assert registry.ensure_workspace("worker") == workspace
    assert workspace.stat().st_mode & 0o777 == 0o700


def test_default_agent_cwd_is_private_workspace_but_explicit_cwd_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    explicit = tmp_path / "repo"
    explicit.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))

    assert _cli_owner_1._resolved_agent_cwd("worker", None) == home / "agents" / "worker" / "workspace"
    assert _cli_owner_1._resolved_agent_cwd("worker", explicit) == explicit.resolve()


def test_agent_destroy_cli_prompts_with_workspace_inventory_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)

    def request(method: str, _params: object = None) -> dict[str, object]:
        calls.append(method)
        if method == "agent.destroy.preview":
            return {
                "ok": True,
                "workspace": {
                    "path": "/isolated/agents/worker/workspace",
                    "exists": True,
                    "files": 2,
                    "bytes": 17,
                    "unreadableEntries": 0,
                },
            }
        return {
            "ok": True,
            "destroyed": True,
            "workspace": {"files": 2, "bytes": 17, "deleted": True},
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: True)
    result = CliRunner().invoke(_cli_root_app, ["agent", "destroy", "worker"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "将删除 workspace(2 个文件、17 字节)" in result.output
    assert calls == ["agent.destroy.preview", "agent.destroy"]


def test_agent_destroy_cli_noninteractive_requires_yes_without_daemon_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(_cli_get_services(), "_stdin_isatty", lambda: False)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *_args, **_kwargs: calls.append(method),
    )

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "destroy", "worker", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["code"] == "CONFIRMATION_REQUIRED"
    assert "--yes" in result.output
    assert calls == []


def test_agent_destroy_cli_yes_skips_prompt_and_reports_deleted_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)

    def request(method: str, _params: object = None) -> dict[str, object]:
        calls.append(method)
        return {
            "ok": True,
            "destroyed": True,
            "workspace": {"files": 3, "bytes": 21, "deleted": True},
        }

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    result = CliRunner().invoke(
        _cli_root_app, ["agent", "destroy", "worker", "--yes", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["workspace"] == {
        "files": 3,
        "bytes": 21,
        "deleted": True,
    }
    assert "将删除" not in result.output
    assert calls == ["agent.destroy"]


def test_t02_rejects_unowned_symlink_and_non_directory_before_registry_insert(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    agents = tmp_path / "home" / "agents"
    agents.mkdir(mode=0o700)
    (agents / "residue").mkdir(mode=0o700)

    with pytest.raises(AgentHomeError) as residue:
        registry.create("residue")
    assert residue.value.category == "unowned-residue"
    assert registry.get("residue") is None

    outside = tmp_path / "outside"
    outside.mkdir()
    (agents / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AgentHomeError):
        registry.create("linked")
    assert registry.get("linked") is None

    (agents / "plain").write_text("not a directory")
    with pytest.raises(AgentHomeError):
        registry.create("plain")
    assert registry.get("plain") is None


def test_t02_real_case_insensitive_alias_reservation_admits_exactly_one(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "case-probe"
    probe.mkdir()
    (probe / "Alice").mkdir()
    if not (probe / "alice").exists():
        pytest.skip("T02 case-insensitive filesystem scenario NOT_RUN on this volume")

    first = _registry(tmp_path)
    second = _track(
        AgentRegistry(
            first.database,
            owner="owner",
            machine="node",
            hyprial_home=tmp_path / "home",
        )
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def create(registry: AgentRegistry, name: str) -> None:
        barrier.wait()
        try:
            registry.create(name)
        except AgentHomeError as error:
            outcomes.append(error.category)
        else:
            outcomes.append("created")

    threads = (
        threading.Thread(target=create, args=(first, "Alice")),
        threading.Thread(target=create, args=(second, "alice")),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    assert sorted(outcomes) == ["created", "name-collision"]
    assert len(first.list()) == 1


def _require_case_insensitive_volume(tmp_path: Path) -> None:
    probe = tmp_path / "case-probe"
    probe.mkdir()
    (probe / "Alice").mkdir()
    if not (probe / "alice").exists():
        pytest.skip("T02 case-insensitive filesystem scenario NOT_RUN on this volume")


def test_t02_a_same_name_created_before_mkdir_is_residue_not_oserror(
    tmp_path: Path,
) -> None:
    """The losing side of a same-name creation race speaks AgentHomeError.

    ``mkdir`` is the call that decides the race; the loser must be classified,
    not leak ``FileExistsError`` out of a lane whose contract is the sanitized
    category error.
    """

    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    provisioner._prepare_root("alice")
    (provisioner.agents_root / "alice").mkdir(mode=0o700)

    with pytest.raises(AgentHomeError) as residue:
        provisioner._create_home_directory("alice", provisioner.agents_root / "alice")
    assert residue.value.category == "unowned-residue"


def test_t02_an_alias_created_before_mkdir_is_a_collision_not_oserror(
    tmp_path: Path,
) -> None:
    _require_case_insensitive_volume(tmp_path)
    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    provisioner._prepare_root("alice")
    (provisioner.agents_root / "Alice").mkdir(mode=0o700)

    with pytest.raises(AgentHomeError) as collision:
        provisioner._create_home_directory("alice", provisioner.agents_root / "alice")
    assert collision.value.category == "name-collision"


def test_t02_an_alias_that_lands_after_the_scan_is_still_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scan and the existence test are not atomic; the second read decides.

    Repro: the alias scan ran before the other spelling landed, so it saw an
    empty root; by the time the path is tested the directory is there.  A
    case-insensitive volume answers True for either spelling, so the entry --
    not the test -- has to name the winner.
    """

    _require_case_insensitive_volume(tmp_path)
    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    provisioner._prepare_root("alice")
    (provisioner.agents_root / "Alice").mkdir(mode=0o700)
    real = provisioner._filesystem_alias
    target = provisioner.agents_root / "alice"
    blinded: list[int] = []

    def once_blind(actor: str, candidate: Path) -> Path | None:
        if not blinded and candidate == target:
            blinded.append(1)
            return None  # exactly what the scan saw before the alias landed
        return real(actor, candidate)

    monkeypatch.setattr(provisioner, "_filesystem_alias", once_blind)

    with pytest.raises(AgentHomeError) as collision:
        registry.create("alice")
    assert blinded == [1]
    assert collision.value.category == "name-collision"
    assert registry.get("alice") is None


def test_t02_prepare_root_tolerates_a_root_that_appears_before_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two provisions creating H/agents at once is routine, not an OSError.

    The same check-then-create shape guards the agents root; the loser here is
    the one that found no root and then lost the ``mkdir``.
    """

    registry = _registry(tmp_path)
    provisioner = registry._home
    assert provisioner is not None
    provisioner._prepare_root("incumbent")
    root = provisioner.agents_root
    real_exists = Path.exists
    blinded: list[int] = []

    def blind_once(self: Path) -> bool:
        if not blinded and self == root:
            blinded.append(1)
            return False  # the other provision created it here
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", blind_once)
    provisioner._prepare_root("candidate")

    assert blinded == [1]
    assert root.is_dir()



def test_t02_sql_failure_compensates_only_the_matching_new_home(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    incumbent = registry.create("incumbent")
    forged = Agent(
        uri=incumbent.uri,
        actor="candidate",
        owner="owner",
        machine="node",
    )

    with pytest.raises(AgentExistsError):
        registry._create_record(forged)

    assert not (tmp_path / "home" / "agents" / "candidate").exists()
    assert registry.home_receipt("incumbent").actor == "incumbent"


def test_t02_sql_failure_compensates_an_empty_optional_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    incumbent = registry.create("incumbent")
    forged = Agent(
        uri=incumbent.uri,
        actor="candidate",
        owner="owner",
        machine="node",
    )
    provisioner = registry._home
    assert provisioner is not None
    provision = provisioner.provision

    def provision_with_workspace(**kwargs: object):  # type: ignore[no-untyped-def]
        attempt = provision(**kwargs)  # type: ignore[arg-type]
        (Path(attempt.receipt.path) / "workspace").mkdir(mode=0o700)
        return attempt

    monkeypatch.setattr(provisioner, "provision", provision_with_workspace)

    with pytest.raises(AgentExistsError):
        registry._create_record(forged)

    assert not (tmp_path / "home" / "agents" / "candidate").exists()
