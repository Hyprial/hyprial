"""Local owner store (J22), personal-org removal (J10), and binding.json guard (J21)."""
from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import OrgMembership, org_space_name

OWNER = "owner@example"
SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "hyprial"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _owner_scope_cas_worker(home_str: str, owner: str, rel: str, expect_version: str,
                            value: str, tag: str, sync_dir_str: str) -> None:
    from hyprial.daemon.impl.org.membership import LocalOwnerStore

    sync_dir = Path(sync_dir_str)
    scope = LocalOwnerStore(Path(home_str)).open(owner)
    (sync_dir / f"{tag}.ready").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 10.0
    while not (sync_dir / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        version = scope.write_text(rel, value, expect_version=expect_version)
        outcome = [value, "ok", version]
    except OrgFsError as error:
        outcome = [value, error.code, None]
    (sync_dir / f"{tag}.json").write_text(json.dumps(outcome), encoding="utf-8")


def test_personal_org_symbols_and_module_are_removed() -> None:
    import hyprial.daemon as daemon
    import hyprial.daemon.impl.org.membership as membership

    for symbol in (
        "ORG_KIND_PERSONAL",
        "PERSONAL_ORG_PREFIX",
        "personal_org_for",
    ):
        assert not hasattr(OrgMembership, symbol), symbol

    for symbol in (
        "PersonalOrgService",
        "PersonalOrgResult",
        "PersonalOrgSpace",
        "PersonalOrgOwnerStore",
        "personal_org_for",
        "resolve_personal_org",
        "ensure_personal_org",
        "OWNER_SCOPE_PERSONAL_ORG",
    ):
        assert not hasattr(membership, symbol), symbol
        assert not hasattr(daemon, symbol), symbol
        assert symbol not in daemon.__all__

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.daemon.impl.org.membership.personal")

    with pytest.raises(ValueError):
        org_space_name("personal-owner-example")


def test_local_owner_store_uses_hyprial_home_state_and_exports_public_face(tmp_path: Path) -> None:
    import hyprial.daemon as daemon
    from hyprial.daemon.impl.org.membership import (
        OWNER_SCOPE_LOCAL,
        LocalOwnerStore,
        OwnerScope,
        OwnerStorePort,
        OwnerStoreUnavailable,
    )

    store = LocalOwnerStore(tmp_path)
    assert isinstance(store, OwnerStorePort)
    assert daemon.LocalOwnerStore is LocalOwnerStore
    assert daemon.OWNER_SCOPE_LOCAL == OWNER_SCOPE_LOCAL
    assert {
        "OwnerStorePort",
        "OwnerScope",
        "OwnerStoreUnavailable",
        "LocalOwnerStore",
        "OWNER_SCOPE_LOCAL",
    } <= set(daemon.__all__)

    with pytest.raises(OwnerStoreUnavailable):
        store.open("")

    scope = store.open("user:" + OWNER)
    assert isinstance(scope, OwnerScope)
    assert (scope.owner, scope.kind) == (OWNER, OWNER_SCOPE_LOCAL)
    assert scope.root.is_relative_to(tmp_path / "state")

    scope.mkdir("directory/provider-keys/k1")
    v1 = scope.write_text("directory/provider-keys/k1/meta.json", '{"seq":1}', create_only=True)
    assert scope.read_text("directory/provider-keys/k1/meta.json") == ('{"seq":1}', v1)
    assert [node.name for node in scope.listdir("directory/provider-keys")] == ["k1"]

    for escape in ("/directory/x.json", "directory/../x.json", "", "directory//x.json"):
        with pytest.raises(ValueError):
            scope.read_text(escape)


def test_source_removes_legacy_binding_json_and_personal_org_markers_and_keeps_machine_binding() -> None:
    machine_binding = SRC_ROOT / "identity" / "impl" / "device" / "machine_binding.py"
    assert machine_binding.is_file()
    assert "state/tailcat/machine-binding.json" in machine_binding.read_text(encoding="utf-8")

    forbidden = (
        "ORG_KIND_PERSONAL",
        "personal_org_for",
        "ensure_personal_org",
        "resolve_personal_org",
        "AGENT_LAST_ORG",
        "AGENT_PERSONAL_ORG",
    )
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path != machine_binding:
            for line in text.splitlines():
                if "binding.json" in line:
                    assert "machine-binding.json" in line, f"{path}: {line}"
        for marker in forbidden:
            assert marker not in text, f"{path} still references {marker}"


def test_owner_store_cas_stat_version_permissions_and_symlinks(tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.membership import LocalOwnerStore, OwnerStoreUnavailable

    missing_home = tmp_path / "missing-home"
    with pytest.raises(OwnerStoreUnavailable):
        LocalOwnerStore(missing_home).open("user:" + OWNER)
    assert not missing_home.exists()

    loose_home = tmp_path / "loose-home"
    loose_home.mkdir(mode=0o755)
    os.chmod(loose_home, 0o755)
    with pytest.raises(OwnerStoreUnavailable):
        LocalOwnerStore(loose_home).open("user:" + OWNER)
    assert not (loose_home / "state").exists()

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    owner_key = OrgMembership.user_key("user:" + OWNER)
    pre_owner = home / "state" / "owner-store" / owner_key
    pre_owner.mkdir(parents=True, mode=0o755)
    os.chmod(pre_owner, 0o755)
    os.chmod(home / "state" / "owner-store", 0o755)
    part_dir = pre_owner / "directory" / "provider-keys" / "k1"
    part_dir.mkdir(parents=True, mode=0o755)
    os.chmod(part_dir, 0o755)

    scope = LocalOwnerStore(home).open("user:" + OWNER)
    assert stat.S_IMODE((home / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "state" / "owner-store").stat().st_mode) == 0o700
    assert stat.S_IMODE(pre_owner.stat().st_mode) == 0o700

    scope.mkdir("directory/provider-keys/k1")
    assert stat.S_IMODE(part_dir.stat().st_mode) == 0o700

    rel = "directory/provider-keys/k1/meta.json"
    v1 = scope.write_text(rel, '{"seq":1}', create_only=True)
    assert stat.S_IMODE((pre_owner / rel).stat().st_mode) == 0o600
    stat_node = scope.stat(rel)
    assert stat_node.version == v1 and v1 != ""
    listed = {node.name: node for node in scope.listdir("directory/provider-keys/k1")}
    assert listed["meta.json"].version == v1

    v2 = scope.write_text(rel, '{"seq":2}', expect_version=stat_node.version)
    assert v2 != v1
    with pytest.raises(OrgFsError) as stale:
        scope.write_text(rel, '{"seq":3}', expect_version=v1)
    assert stale.value.code == "stale-write"

    sync_dir = tmp_path / "sync"
    sync_dir.mkdir(mode=0o700)
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_owner_scope_cas_worker,
                     args=(str(home), "user:" + OWNER, rel, v2, '{"seq":"p1"}', "p1", str(sync_dir)))
    p2 = ctx.Process(target=_owner_scope_cas_worker,
                     args=(str(home), "user:" + OWNER, rel, v2, '{"seq":"p2"}', "p2", str(sync_dir)))
    p1.start()
    p2.start()
    try:
        deadline = time.monotonic() + 10.0
        while not ((sync_dir / "p1.ready").exists() and (sync_dir / "p2.ready").exists()):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        (sync_dir / "go").write_text("1", encoding="utf-8")
        p1.join(timeout=15)
        p2.join(timeout=15)
        assert p1.exitcode == 0 and p2.exitcode == 0
    finally:
        p1.close()
        p2.close()
    outcomes = [
        json.loads((sync_dir / "p1.json").read_text(encoding="utf-8")),
        json.loads((sync_dir / "p2.json").read_text(encoding="utf-8")),
    ]
    winners = [item for item in outcomes if item[1] == "ok"]
    losers = [item for item in outcomes if item[1] == "stale-write"]
    assert len(winners) == 1 and len(losers) == 1
    assert scope.read_text(rel) == (winners[0][0], winners[0][2])

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        scope.read_text("directory\\..\\x.json")
    (pre_owner / "directory" / "provider-keys" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OrgFsError):
        scope.write_text("directory/provider-keys/escape/meta.json", "x", create_only=True)
    with pytest.raises(OrgFsError):
        scope.stat("directory/provider-keys/escape/meta.json")
    with pytest.raises(OrgFsError):
        scope.listdir("directory/provider-keys/escape")
    assert list(outside.iterdir()) == []

    sym_home = tmp_path / "sym-home"
    sym_home.mkdir(mode=0o700)
    (sym_home / "state").mkdir(mode=0o700)
    (sym_home / "state" / "owner-store").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OwnerStoreUnavailable):
        LocalOwnerStore(sym_home).open("user:" + OWNER)
    assert list(outside.iterdir()) == []


def test_owner_store_hardens_existing_nested_dirs_and_cas_lock_on_read_stat_list(tmp_path: Path) -> None:
    from hyprial.daemon.impl.org.membership import LocalOwnerStore

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    scope = LocalOwnerStore(home).open("user:" + OWNER)
    rel = "directory/provider-keys/k1/meta.json"
    v1 = scope.write_text(rel, '{"seq":1}', create_only=True)
    scope.mkdir("directory/provider-keys/k2")

    dir_layer = scope.root / "directory"
    keys_dir = dir_layer / "provider-keys"
    part_dir = keys_dir / "k1"
    child_dir = keys_dir / "k2"
    lock_file = scope.root / ".cas.lock"

    for path in (dir_layer, keys_dir, part_dir, child_dir):
        os.chmod(path, 0o755)
    os.chmod(lock_file, 0o644)

    assert scope.read_text(rel) == ('{"seq":1}', v1)
    for path in (dir_layer, keys_dir, part_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    for path in (keys_dir, part_dir, child_dir):
        os.chmod(path, 0o755)
    os.chmod(lock_file, 0o644)

    assert scope.stat(rel).version == v1
    assert stat.S_IMODE(part_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    os.chmod(part_dir, 0o755)
    assert scope.stat("directory/provider-keys/k1").kind == "dir"
    assert stat.S_IMODE(part_dir.stat().st_mode) == 0o700

    for path in (keys_dir, part_dir, child_dir):
        os.chmod(path, 0o755)
    os.chmod(lock_file, 0o644)

    listed = {node.name: node for node in scope.listdir("directory/provider-keys")}
    assert set(listed) == {"k1", "k2"}
    assert [node.name for node in scope.listdir("directory/provider-keys/k1")] == ["meta.json"]
    for path in (keys_dir, part_dir, child_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    os.chmod(lock_file, 0o644)
    v2 = scope.write_text(rel, '{"seq":2}', expect_version=v1)
    assert v2 != v1
    assert stat.S_IMODE(lock_file.stat().st_mode) == 0o600

    outside_lock = tmp_path / "outside-lock"
    outside_lock.write_text("x", encoding="utf-8")
    os.chmod(outside_lock, 0o644)
    lock_file.unlink()
    lock_file.symlink_to(outside_lock)
    for call in (
        lambda: scope.read_text(rel),
        lambda: scope.stat(rel),
        lambda: scope.listdir("directory/provider-keys/k1"),
        lambda: scope.write_text(rel, '{"seq":3}', expect_version=v2),
    ):
        with pytest.raises(OrgFsError) as caught:
            call()
        assert caught.value.code == "unavailable"
    assert stat.S_IMODE(outside_lock.stat().st_mode) == 0o644


def test_non_worm_sources_reject_retired_personal_org_terms() -> None:
    retired = (
        "personal-example",
        "personal-only",
        "org.personal.ensure",
        "ORG_KIND_PERSONAL",
        "personal_org_for",
        "ensure_personal_org",
        "resolve_personal_org",
        "AGENT_LAST_ORG",
        "AGENT_PERSONAL_ORG",
    )
    active_gui_docs = (
        REPO_ROOT / "gui" / "docs" / "modules" / "dev-04.md",
        REPO_ROOT / "gui" / "docs" / "org" / "interaction-design.zh-CN.md",
        REPO_ROOT / "gui" / "docs" / "org" / "prd.zh-CN.md",
        REPO_ROOT / "gui" / "docs" / "business-lines" / "org-members.zh-CN.md",
        REPO_ROOT / "docs" / "gui" / "org" / "index.html",
    )
    for doc in active_gui_docs:
        assert not doc.is_relative_to(REPO_ROOT / "docs" / "notes")
    targets = (
        *sorted((REPO_ROOT / "src" / "hyprial").rglob("*.py")),
        *sorted((REPO_ROOT / "gui" / "src").rglob("*.ts")),
        *sorted((REPO_ROOT / "gui" / "src").rglob("*.tsx")),
        REPO_ROOT / "contract" / "daemon-request" / "openapi.json",
        REPO_ROOT / "contract" / "daemon-request" / "README.md",
        REPO_ROOT / "contract" / "org-network" / "membership.md",
        REPO_ROOT / "gui" / "src" / "dev" / "contract-mock" / "contract.generated.json",
        *active_gui_docs,
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for term in retired:
            assert term not in text, f"{path} still references retired term {term}"
    retired_doc_terms = (
        "个人组织",
        "personal organization",
        "我的组织（个人）",
        "work/personal",
        "兜底归属",
        "至少属于一个组织",
    )
    for doc in active_gui_docs:
        text = doc.read_text(encoding="utf-8")
        for term in retired_doc_terms:
            assert term not in text, (
                f"{doc} still prescribes the retired org model via term {term}"
            )
