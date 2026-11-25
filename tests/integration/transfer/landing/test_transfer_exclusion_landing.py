# Decision record (file header; no inline comments by repository rule):
# - OC5 P1-3 RED/GREEN regression tests for carried exclusions validation and
#   rollback fences during land_bundle_into_registry.
"""OC5 carried org-agent-exclusions landing validation and rollback tests."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.network.member_agents.exclusions import (
    TRANSFER_EXCLUSIONS_RELPATH,
    OrgAgentExclusion,
    OrgAgentExclusionStore,
)
from hyprial.daemon.impl.transfer.landing import registry as landing_registry
from hyprial.daemon.impl.transfer.landing.registry import (
    LandingError,
    LandingFailed,
    _adopt_carried_org_exclusions,
    land_bundle_into_registry,
)
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.kernel import canonical_user_uri, current_uid
from tests.integration.transfer.landing.test_transfer_registry_landing import (
    ACTOR,
    OWNER,
    _bundle,
    _registry,
    _source_home,
)

SOURCE_AGENT_ID = "a." + "c" * 26


def _valid_carried_payload() -> str:
    return json.dumps(
        [
            {"agentId": SOURCE_AGENT_ID, "directorySpace": "space-111"},
            {"agentId": SOURCE_AGENT_ID, "directorySpace": "space-222"},
        ]
    )


def test_malformed_carried_exclusions_fails_before_confirm_without_confirmed_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    carried = home / TRANSFER_EXCLUSIONS_RELPATH
    carried.write_text("{not-valid-json", encoding="utf-8")
    carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    confirm_calls: list[str] = []
    original_confirm = AgentRegistry.confirm_landing_home

    def spy_confirm(self: AgentRegistry, actor: str, *, expected_entity_token: str) -> str:
        confirm_calls.append(actor)
        return original_confirm(self, actor, expected_entity_token=expected_entity_token)

    monkeypatch.setattr(AgentRegistry, "confirm_landing_home", spy_confirm)
    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-malformed",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert confirm_calls == []
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-malformed").exists()
        assert OrgAgentExclusionStore(target_home).list() == ()
    finally:
        registry.close()


def test_carried_exclusions_helper_preserves_carried_file_when_store_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    try:
        created = registry.create(ACTOR)
        dest = target_home / "agents" / ACTOR
        carried = dest / TRANSFER_EXCLUSIONS_RELPATH
        carried.parent.mkdir(parents=True, exist_ok=True)
        carried.write_text(_valid_carried_payload(), encoding="utf-8")
        carried.chmod(0o600)

        def fail_adopt(*_args: object, **_kwargs: object) -> bool:
            raise OSError("disk full")

        monkeypatch.setattr(OrgAgentExclusionStore, "adopt_for_agent", fail_adopt)
        with pytest.raises(OSError, match="disk full"):
            _adopt_carried_org_exclusions(
                dest=dest,
                machine_home=target_home,
                registry=registry,
                actor=ACTOR,
            )

        assert carried.exists()
        assert carried.read_text(encoding="utf-8") == _valid_carried_payload()
        assert OrgAgentExclusionStore(target_home).excluded_spaces(created.agent_id) == frozenset()
    finally:
        registry.close()


def test_landing_rolls_back_agent_and_preserves_bundle_when_exclusion_store_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    carried_exists_on_failure: list[bool] = []
    dest_carried = target_home / "agents" / ACTOR / TRANSFER_EXCLUSIONS_RELPATH

    def fail_adopt(*_args: object, **_kwargs: object) -> bool:
        carried_exists_on_failure.append(dest_carried.exists())
        raise OSError("simulated store write failure")

    monkeypatch.setattr(OrgAgentExclusionStore, "adopt_for_agent", fail_adopt)
    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-store-fail",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert carried_exists_on_failure == [True]
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-store-fail").exists()
        assert OrgAgentExclusionStore(target_home).list() == ()
        assert (bundle_dir / "payload" / TRANSFER_EXCLUSIONS_RELPATH).exists()
    finally:
        registry.close()


def test_landing_rolls_back_written_exclusions_when_post_adopt_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    original_unlink = Path.unlink
    failed_once = False

    def fail_carried_unlink_once(self: Path, missing_ok: bool = False) -> None:
        nonlocal failed_once
        if (
            not failed_once
            and self.name == "org-agent-exclusions.json"
            and "agents" in self.parts
        ):
            failed_once = True
            raise OSError("cannot unlink carried file")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_carried_unlink_once)
    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-unlink-fail",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert failed_once is True
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert OrgAgentExclusionStore(target_home).list() == ()
    finally:
        registry.close()


def test_landing_refuses_broad_mode_carried_exclusions_before_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o644)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    confirm_calls: list[str] = []
    original_confirm = AgentRegistry.confirm_landing_home

    def spy_confirm(self: AgentRegistry, actor: str, *, expected_entity_token: str) -> str:
        confirm_calls.append(actor)
        return original_confirm(self, actor, expected_entity_token=expected_entity_token)

    monkeypatch.setattr(AgentRegistry, "confirm_landing_home", spy_confirm)
    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-broad-mode",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert confirm_calls == []
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-broad-mode").exists()

        with pytest.raises(LandingError, match="carried exclusions file"):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-broad-dry-run",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
                dry_run=True,
            )
    finally:
        registry.close()


def test_landing_rolls_back_exclusions_when_post_publish_dir_fsync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.org.network.member_agents.exclusions as mod
    from hyprial.kernel.impl.manysys.fs import private_access

    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    other_agent_id = "a." + "d" * 26
    OrgAgentExclusionStore(target_home).exclude(other_agent_id, "space-existing")

    registry = _registry(target_home)
    state_dir = target_home / "state"
    exclusions_file = state_dir / "org-agent-exclusions.json"
    published_before_failure: list[bool] = []
    original_fsync = private_access.fsync_directory
    failed_once = False

    def fail_post_publish_fsync(path: Path | str) -> bool:
        nonlocal failed_once
        if (
            not failed_once
            and Path(path) == state_dir
            and exclusions_file.exists()
            and "space-111" in exclusions_file.read_text(encoding="utf-8")
        ):
            failed_once = True
            published_before_failure.append(True)
            raise OSError("simulated post-publish dir fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(private_access, "fsync_directory", fail_post_publish_fsync)
    if hasattr(mod, "fsync_directory"):
        monkeypatch.setattr(mod, "fsync_directory", fail_post_publish_fsync)

    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-fsync-fail",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert published_before_failure == [True]
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-fsync-fail").exists()
        assert OrgAgentExclusionStore(target_home).list() == (
            OrgAgentExclusion(other_agent_id, "space-existing"),
        )
        assert (bundle_dir / "payload" / TRANSFER_EXCLUSIONS_RELPATH).exists()
    finally:
        registry.close()


def test_adopt_for_agent_returns_exact_delta_and_concurrent_writer_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.org.network.member_agents.exclusions as mod
    from hyprial.kernel.impl.manysys.fs import private_access

    home = tmp_path / "concurrent-home"
    home.mkdir(mode=0o700)
    store = OrgAgentExclusionStore(home)
    agent_id = "a." + "e" * 26

    entered_lock = threading.Event()
    allow_thread1_finish = threading.Event()
    thread1_ident: list[int] = []
    errors: list[BaseException] = []
    outcomes: list[object] = []

    orig_load = OrgAgentExclusionStore._load_locked

    def gated_load(self: OrgAgentExclusionStore) -> list[OrgAgentExclusion]:
        if thread1_ident and threading.get_ident() == thread1_ident[0]:
            entered_lock.set()
            assert allow_thread1_finish.wait(timeout=5.0)
        return orig_load(self)

    monkeypatch.setattr(OrgAgentExclusionStore, "_load_locked", gated_load)

    def writer_one() -> None:
        thread1_ident.append(threading.get_ident())
        try:
            store.exclude(agent_id, "space-111")
        except BaseException as exc:
            errors.append(exc)

    def adopter_two() -> None:
        try:
            outcomes.append(store.adopt_for_agent(agent_id, ("space-111", "space-222")))
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer_one)
    t1.start()
    assert entered_lock.wait(timeout=5.0)
    t2 = threading.Thread(target=adopter_two)
    t2.start()
    allow_thread1_finish.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert not errors
    assert len(outcomes) == 1
    adoption = outcomes[0]
    assert adoption.agent_id == agent_id
    assert adoption.added_spaces == ("space-222",)
    assert adoption.changed is True
    assert bool(adoption) is True
    assert store.excluded_spaces(agent_id) == frozenset({"space-111", "space-222"})
    assert adoption.undo() is True
    assert store.excluded_spaces(agent_id) == frozenset({"space-111"})

    monkeypatch.setattr(OrgAgentExclusionStore, "_load_locked", orig_load)
    committed_seen: list[object] = []
    original_fsync = private_access.fsync_directory
    fail_once = True

    def fail_dir_fsync_once(path: Path | str) -> bool:
        nonlocal fail_once
        if fail_once and Path(path) == home / "state":
            fail_once = False
            raise OSError("post-publish dir fsync failed")
        return original_fsync(path)

    monkeypatch.setattr(private_access, "fsync_directory", fail_dir_fsync_once)
    if hasattr(mod, "fsync_directory"):
        monkeypatch.setattr(mod, "fsync_directory", fail_dir_fsync_once)

    with pytest.raises(mod.OrgAgentExclusionStorageError) as caught:
        store.adopt_for_agent(
            agent_id,
            ("space-333",),
            on_committed=committed_seen.append,
        )

    assert len(committed_seen) == 1
    assert caught.value.committed_adoption == committed_seen[0]
    assert caught.value.committed_adoption.added_spaces == ("space-333",)
    assert store.excluded_spaces(agent_id) == frozenset({"space-111", "space-333"})
    assert caught.value.committed_adoption.undo() is True
    assert store.excluded_spaces(agent_id) == frozenset({"space-111"})


def test_landing_rollback_preserves_concurrent_writer_exclusion_when_unlink_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)

    orig_adopt = OrgAgentExclusionStore.adopt_for_agent
    orig_unlink = Path.unlink
    raced_agent_ids: list[str] = []
    raced_once = False
    unlink_failed_once = False

    def racing_adopt(
        self: OrgAgentExclusionStore,
        agent_id: str,
        directory_spaces: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal raced_once
        if not raced_once:
            raced_once = True
            raced_agent_ids.append(agent_id)
            OrgAgentExclusionStore(target_home).exclude(agent_id, "space-111")
        return orig_adopt(self, agent_id, directory_spaces, *args, **kwargs)

    def fail_carried_unlink_once(self: Path, missing_ok: bool = False) -> None:
        nonlocal unlink_failed_once
        if (
            not unlink_failed_once
            and self.name == "org-agent-exclusions.json"
            and "agents" in self.parts
        ):
            unlink_failed_once = True
            raise OSError("cannot unlink carried file")
        orig_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(OrgAgentExclusionStore, "adopt_for_agent", racing_adopt)
    monkeypatch.setattr(Path, "unlink", fail_carried_unlink_once)

    try:
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-concurrent-unlink-fail",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
            )

        assert raced_once is True
        assert unlink_failed_once is True
        assert len(raced_agent_ids) == 1
        assert registry.exists(ACTOR) is False
        assert not (target_home / "agents" / ACTOR).exists()
        assert OrgAgentExclusionStore(target_home).excluded_spaces(
            raced_agent_ids[0]
        ) == frozenset({"space-111"})
    finally:
        registry.close()


def test_landing_without_machine_home_preserves_carried_exclusions_file(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    try:
        landed = land_bundle_into_registry(
            bundle_dir,
            registry=registry,
            home_root=target_home / "agents",
            machine_home=None,
            staging=tmp_path / "staging" / "landing-no-machine-home",
            granted_by=canonical_user_uri(OWNER),
            expected_actor=ACTOR,
        )

        assert registry.exists(ACTOR) is True
        carried = landed.destination / TRANSFER_EXCLUSIONS_RELPATH
        assert carried.exists() is True
        assert carried.read_text(encoding="utf-8") == _valid_carried_payload()
        assert OrgAgentExclusionStore(target_home).list() == ()
    finally:
        registry.close()


def test_landing_empty_carried_exclusions_preserves_carried_file(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text("[]", encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    try:
        landed = land_bundle_into_registry(
            bundle_dir,
            registry=registry,
            home_root=target_home / "agents",
            machine_home=target_home,
            staging=tmp_path / "staging" / "landing-empty-carried",
            granted_by=canonical_user_uri(OWNER),
            expected_actor=ACTOR,
        )

        assert registry.exists(ACTOR) is True
        carried = landed.destination / TRANSFER_EXCLUSIONS_RELPATH
        assert carried.exists() is True
        assert carried.read_text(encoding="utf-8") == "[]"
        assert OrgAgentExclusionStore(target_home).list() == ()
    finally:
        registry.close()


def _patch_wrong_owner(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    real = target.lstat()
    target_id = (real.st_dev, real.st_ino)
    fake_uid = current_uid() + 1
    orig_lstat = Path.lstat
    orig_fstat = os.fstat

    def _with_fake_uid(st: os.stat_result) -> os.stat_result:
        if (st.st_dev, st.st_ino) != target_id:
            return st
        return os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                fake_uid,
                st.st_gid,
                st.st_size,
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            )
        )

    monkeypatch.setattr(Path, "lstat", lambda self: _with_fake_uid(orig_lstat(self)))
    monkeypatch.setattr(os, "fstat", lambda fd: _with_fake_uid(orig_fstat(fd)))


def test_dry_run_accepts_unmodified_export_bundle_with_carried_exclusions(
    tmp_path: Path,
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    assert stat.S_IMODE((bundle_dir / "payload").stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle_dir / "payload" / "state").stat().st_mode) == 0o700
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)
    try:
        preview = land_bundle_into_registry(
            bundle_dir,
            registry=registry,
            home_root=target_home / "agents",
            machine_home=target_home,
            staging=tmp_path / "staging" / "dry-run-valid",
            granted_by=canonical_user_uri(OWNER),
            expected_actor=ACTOR,
            dry_run=True,
        )
        assert preview.dry_run is True
        assert preview.actor == ACTOR
        assert registry.exists(ACTOR) is False
        assert OrgAgentExclusionStore(target_home).list() == ()

        (bundle_dir / "payload").chmod(0o755)
        (bundle_dir / "payload" / "state").chmod(0o755)
        broad_dir_preview = land_bundle_into_registry(
            bundle_dir,
            registry=registry,
            home_root=target_home / "agents",
            machine_home=target_home,
            staging=tmp_path / "staging" / "dry-run-broad-dir",
            granted_by=canonical_user_uri(OWNER),
            expected_actor=ACTOR,
            dry_run=True,
        )
        assert broad_dir_preview.dry_run is True
        assert broad_dir_preview.actor == ACTOR
        assert registry.exists(ACTOR) is False
        assert OrgAgentExclusionStore(target_home).list() == ()
    finally:
        registry.close()


def test_landing_refuses_hardlinked_or_wrong_owner_bundle_carried_exclusions_before_receive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _source_home(tmp_path)
    src_carried = home / TRANSFER_EXCLUSIONS_RELPATH
    src_carried.write_text(_valid_carried_payload(), encoding="utf-8")
    src_carried.chmod(0o600)
    bundle_dir = _bundle(tmp_path, home)
    bundle_carried = bundle_dir / "payload" / TRANSFER_EXCLUSIONS_RELPATH
    target_home = tmp_path / "target-home"
    target_home.mkdir(mode=0o700)
    registry = _registry(target_home)

    receive_calls: list[Path] = []
    original_receive = landing_registry.receive_bundle

    def spy_receive(bundle_arg: object, *args: object, **kwargs: object) -> object:
        receive_calls.append(Path(str(bundle_arg)))
        return original_receive(bundle_arg, *args, **kwargs)

    monkeypatch.setattr(landing_registry, "receive_bundle", spy_receive)
    hardlink = tmp_path / "hardlink-exclusions"
    os.link(bundle_carried, hardlink)
    try:
        staging_hl = tmp_path / "staging" / "landing-hardlink"
        with pytest.raises(LandingFailed):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=staging_hl,
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
                dry_run=False,
            )
        assert receive_calls == []
        assert not staging_hl.exists()
        assert registry.exists(ACTOR) is False
        assert OrgAgentExclusionStore(target_home).list() == ()

        with pytest.raises(LandingError):
            land_bundle_into_registry(
                bundle_dir,
                registry=registry,
                home_root=target_home / "agents",
                machine_home=target_home,
                staging=tmp_path / "staging" / "landing-hardlink-dry",
                granted_by=canonical_user_uri(OWNER),
                expected_actor=ACTOR,
                dry_run=True,
            )
    finally:
        hardlink.unlink()

    try:
        with monkeypatch.context() as mp:
            _patch_wrong_owner(mp, bundle_carried)
            staging_owner = tmp_path / "staging" / "landing-wrong-owner"
            with pytest.raises(LandingFailed):
                land_bundle_into_registry(
                    bundle_dir,
                    registry=registry,
                    home_root=target_home / "agents",
                    machine_home=target_home,
                    staging=staging_owner,
                    granted_by=canonical_user_uri(OWNER),
                    expected_actor=ACTOR,
                    dry_run=False,
                )
            assert receive_calls == []
            assert not staging_owner.exists()
            assert registry.exists(ACTOR) is False
            assert OrgAgentExclusionStore(target_home).list() == ()

            with pytest.raises(LandingError):
                land_bundle_into_registry(
                    bundle_dir,
                    registry=registry,
                    home_root=target_home / "agents",
                    machine_home=target_home,
                    staging=tmp_path / "staging" / "landing-wrong-owner-dry",
                    granted_by=canonical_user_uri(OWNER),
                    expected_actor=ACTOR,
                    dry_run=True,
                )
    finally:
        registry.close()
