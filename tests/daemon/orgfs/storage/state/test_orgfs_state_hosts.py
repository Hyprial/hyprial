# Decisions (phase D, package T3, 2026-11-25):
# - The state hosts (``StateOrgFs``, ``StateOrgFsRuntime``) are built from their
#   own mixins placed in front of retained legacy mixins (design §9, plan §3.3),
#   so a method name may appear twice among their bases; the rule for them is
#   that each group is free of duplicates and the host resolves every shared
#   name to its own mixin.  The legacy hosts keep the original rule; the legacy
#   facade is no longer named here because the rehearsal switch rebinds it.
"""Responsibility extraction keeps one constructor and durable state host."""

import ast
import inspect
import textwrap

from hyprial.daemon.impl.orgfs.composition.state import StateOrgFsRuntime
from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.document.coordinator import StateOrgFs
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import (
    FileSpaceStore,
    LocalSpaceStore,
    PersistentSpaceStore,
    SQLiteSpaceStore,
)

_STATE_MODULES = (
    "hyprial.daemon.impl.orgfs.document.coordinator",
    "hyprial.daemon.impl.orgfs.composition.state",
)


def test_store_aliases_are_one_canonical_class():
    assert SQLiteSpaceStore is FileSpaceStore is PersistentSpaceStore is LocalSpaceStore


def _responsibility_methods(responsibility: type) -> list[str]:
    return [
        name for name, value in responsibility.__dict__.items()
        if callable(value) or isinstance(value, (property, staticmethod))
    ]


def test_only_canonical_hosts_own_construction():
    for host in (LocalSpaceStore, OrgFsMesh):
        assert "__init__" in host.__dict__
        method_names = []
        for responsibility in host.__bases__:
            assert "__init__" not in responsibility.__dict__
            assert "__getattr__" not in responsibility.__dict__
            method_names.extend(_responsibility_methods(responsibility))
        assert len(method_names) == len(set(method_names))


def test_state_hosts_own_construction_and_their_mixins_win_over_the_retained_ones():
    for host in (StateOrgFs, StateOrgFsRuntime):
        assert "__init__" in host.__dict__
        own: dict[str, type] = {}
        retained: dict[str, type] = {}
        for responsibility in host.__bases__:
            assert "__init__" not in responsibility.__dict__
            assert "__getattr__" not in responsibility.__dict__
            group = own if responsibility.__module__.startswith(_STATE_MODULES) else retained
            for name in _responsibility_methods(responsibility):
                assert name not in group, (host, name)
                group[name] = responsibility
        assert own
        for name in own.keys() & retained.keys():
            assert inspect.getattr_static(host, name) is own[name].__dict__[name], (host, name)


def test_directory_budget_has_one_configuration_owner(monkeypatch, tmp_path):
    from hyprial.daemon.impl.orgfs.composition import directory

    monkeypatch.setattr(directory, "ORGFS_ANNOUNCE_BUFFER_LIMIT", 3)
    runtime = OrgFsRuntime(tmp_path, node_id="node-local", author="user:local")
    try:
        assert runtime._directory_capacity == 4
        assert runtime._directory_mailbox_capacity() == 4
    finally:
        runtime.close()


def test_purge_subflows_keep_the_durable_publication_sequence():
    source = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(LocalOrgFs.purge))))
    calls = [
        node.func.attr for statement in source.body[0].body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("_purge_phase_")
    ]
    assert calls == [
        "_purge_phase_prepare_replacements",
        "_purge_phase_commit_retirements",
        "_purge_phase_install_replacements",
        "_purge_phase_publish_frontiers",
        "_purge_phase_delete_retired_objects",
        "_purge_phase_acknowledge",
    ]
