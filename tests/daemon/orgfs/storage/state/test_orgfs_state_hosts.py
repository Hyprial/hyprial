"""Responsibility extraction keeps one constructor and durable state host."""

import ast
import inspect
import textwrap

from hyprial.daemon.impl.orgfs.docs import LocalOrgFs
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import (
    FileSpaceStore,
    LocalSpaceStore,
    PersistentSpaceStore,
    SQLiteSpaceStore,
)


def test_store_aliases_are_one_canonical_class():
    assert SQLiteSpaceStore is FileSpaceStore is PersistentSpaceStore is LocalSpaceStore


def test_only_canonical_hosts_own_construction():
    for host in (LocalOrgFs, LocalSpaceStore, OrgFsMesh, OrgFsRuntime):
        assert "__init__" in host.__dict__
        method_names = []
        for responsibility in host.__bases__:
            assert "__init__" not in responsibility.__dict__
            assert "__getattr__" not in responsibility.__dict__
            method_names.extend(
                name for name, value in responsibility.__dict__.items()
                if callable(value) or isinstance(value, (property, staticmethod))
            )
        assert len(method_names) == len(set(method_names))


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
