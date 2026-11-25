"""Production settings reads must not depend on test-only registry injection."""

import pytest

from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MIGRATED_FAMILIES,
    daemon_openapi,
)

READS = (
    "autoupdate.status", "upgrade.steps.status", "daemon.status", "log.page",
    "identity.users.list",
    "agent.secret.provider-list",
)
WRITES = (
    "identity.users.add",
    "agent.secret.provider-write", "agent.secret.grant",
    "agent.secret.revoke",
)


@pytest.mark.parametrize("name", READS)
def test_production_reads_are_declared_safe_local_operator_methods(name):
    binding = METHOD_REGISTRY.get(name)
    assert binding is not None, f"production registry is missing {name}"
    spec = binding.spec
    assert (spec.mutates, spec.retry, spec.http_exposed) == (False, "safe", True)
    assert spec.audience == ("local-operator",)
    operation = daemon_openapi()["paths"][f"/daemon/request/{name}"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["examples"]


def test_combined_slice_registers_exact_writes_and_keeps_other_families_open():
    for name in WRITES:
        spec = METHOD_REGISTRY.get(name).spec
        assert spec.mutates and spec.http_exposed
        assert spec.retry == "never"
    assert "identity" in MIGRATED_FAMILIES
    assert not {"agent", "upgrade", "autoupdate"}.intersection(MIGRATED_FAMILIES)
