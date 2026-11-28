"""Membership migration markers and reusable schema validation."""
from dataclasses import replace

import pytest

from hyprial.daemon.impl.request_catalog import DAEMON_CATALOG_SPEC
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.migration import (
    MIGRATED_FAMILIES, MIGRATED_METHODS, validate_migration_coverage,
)
from hyprial.kernel import MethodSpecError, validate_method_spec


def test_the_migration_record_is_precise_per_method() -> None:
    # Migration is per method: every declared name is owned by name, and the
    # family marker only covers a family whose every member already migrated.
    assert set(METHOD_REGISTRY.names) == MIGRATED_METHODS
    for family in MIGRATED_FAMILIES:
        assert family in {name.split(".", 1)[0] for name in METHOD_REGISTRY.names}


def test_a_declaration_without_a_migration_marker_is_rejected() -> None:
    validate_migration_coverage((DAEMON_CATALOG_SPEC,))
    with pytest.raises(ValueError, match="org.unregistered"):
        validate_migration_coverage((replace(DAEMON_CATALOG_SPEC, name="org.unregistered"),))


def test_identical_declared_schemas_are_metaschema_checked_once(monkeypatch) -> None:
    """A shared error vocabulary must not multiply the freeze/export cost.

    org.join and org.invites.accept repeat the same error data schemas many
    times; the metaschema verdict depends only on schema content.
    """

    from hyprial.kernel.impl.contracts.daemon import method_spec

    checks: list[object] = []
    real = method_spec.Draft202012Validator.check_schema

    def counting(schema, *args, **kwargs):
        checks.append(schema)
        return real(schema, *args, **kwargs)

    monkeypatch.setattr(method_spec.Draft202012Validator, "check_schema", counting)
    method_spec._metaschema_error.cache_clear()
    specs = (DAEMON_CATALOG_SPEC, replace(DAEMON_CATALOG_SPEC, name="daemon.catalog.copy"))

    method_spec.validate_method_specs(specs)
    first = len(checks)
    method_spec.validate_method_specs(specs)

    assert len(checks) == first
    with pytest.raises(MethodSpecError, match="params"):
        validate_method_spec(replace(DAEMON_CATALOG_SPEC, params_schema={
            "type": "object", "properties": {}, "additionalProperties": False,
            "minProperties": "zero",
        }))


def test_new_methods_can_be_declared_without_migrating_their_whole_family() -> None:
    assert "message.send.lookup" in METHOD_REGISTRY
    assert "message.history" in METHOD_REGISTRY
    assert "message.conversations" not in METHOD_REGISTRY
    assert "outbox.list" in METHOD_REGISTRY
    assert "outbox" not in MIGRATED_FAMILIES
    assert "message.query" in METHOD_REGISTRY
    assert "message.send" in METHOD_REGISTRY
    assert MIGRATED_FAMILIES == {"daemon", "identity", "orgfs", "user", "userKey"}


def test_orgfs_family_exactly_matches_registered_migrated_and_bridge_methods() -> None:
    from hyprial.daemon.impl.application.messaging.orgfs_bridge import _ORGFS_MUTATING_METHODS, _ORGFS_READ_METHODS
    from hyprial.daemon.impl.request_catalog.org.orgfs import BATCH3_ORGFS_BINDINGS, BATCH4_ORGFS_BINDINGS

    reads = {
        "orgfs.spaces", "orgfs.resolve", "orgfs.ls", "orgfs.stat", "orgfs.read",
        "orgfs.status", "orgfs.trash", "orgfs.watch",
        "orgfs.members",
    }
    writes = {
        "orgfs.create", "orgfs.write", "orgfs.export", "orgfs.import", "orgfs.mkdir", "orgfs.move",
        "orgfs.remove", "orgfs.restore", "orgfs.space_purge",
        "orgfs.checkout", "orgfs.invite", "orgfs.remove_member", "orgfs.join",
    }
    assert len(reads) == 9 and len(writes) == 13 and not reads & writes
    assert {spec.name for spec, _handler in BATCH3_ORGFS_BINDINGS} == reads
    assert {spec.name for spec, _handler in BATCH4_ORGFS_BINDINGS} == writes
    expected = reads | writes
    assert {name for name in METHOD_REGISTRY.names if name.startswith("orgfs.")} == expected
    assert {name for name in MIGRATED_METHODS if name.startswith("orgfs.")} == expected
    assert _ORGFS_READ_METHODS | _ORGFS_MUTATING_METHODS == expected
    for spec, handler in BATCH3_ORGFS_BINDINGS + BATCH4_ORGFS_BINDINGS:
        assert callable(handler) and METHOD_REGISTRY.get(spec.name).handler is handler
    assert "orgfs" in MIGRATED_FAMILIES




def test_every_declared_name_belongs_to_an_explicit_migrated_family() -> None:
    families = {name.split(".", 1)[0] for name in METHOD_REGISTRY.names}
    assert families - MIGRATED_FAMILIES == {
        "adapter",
        "agent",
        "auth",
        "conversations",
        "autoupdate",
        "hosts",
        "log",
        "management",
        "message",
        "network",
        "onboarding",
        "org",
        "outbox",
        "pac",
        "progress",
        "routine",
        "service",
        "session",
        "targets",
        "teleport",
        "top",
        "upgrade",
        "workflow",
    }
    assert "link" not in MIGRATED_FAMILIES
    assert "org" not in MIGRATED_FAMILIES
