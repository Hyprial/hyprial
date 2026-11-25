"""The phase/caller gate and the catalog declaration digest (business surface).

These four cases survived the GUI control-plane removal: `phase_caller_allows`
guards ordinary IPC method dispatch, and the declaration digest is the catalog
parity anchor between the daemon and the request-HTTP projection.  They were
migrated verbatim from the deleted control protocol tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from hyprial.daemon.impl.ipc.protocol.catalog import (
    canonical_digest,
    catalog_result_digest,
    declaration_digest,
    declaration_document,
    phase_caller_allows,
    spec_phase_caller_allows,
)
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.onboarding.specs import (
    BOOTSTRAP_AND_SERVING,
    BOOTSTRAP_ONLY,
)
from hyprial.kernel import export_catalog
from hyprial.shell.impl.request_http.catalog import (
    declaration_digest as http_declaration_digest,
    declaration_document as http_declaration_document,
)

_DYNAMIC = {"declared": True, "state": "available", "callable": True, "reason": None}
_GOLDEN = [
    {
        "name": "zeta.method",
        "owner": "daemon.onboarding",
        "httpExposed": True,
        "httpBinding": {"identity": "none"},
        "mode": "unary",
        "mutates": False,
        "retry": "safe",
        "confirmation": "none",
        "audience": ["local-operator"],
        "paramsSchema": {"type": "object", "properties": {}},
        "resultSchema": {"type": "object"},
        "errors": [{"code": "INVALID_ARGUMENT"}],
        "ipcParamValidation": {"strict": True},
        "declared": False,
        "state": "derived",
        "callable": False,
        "reason": None,
    },
    {
        "name": "alpha.method",
        "owner": "daemon.onboarding",
        "httpExposed": True,
        "httpBinding": {"identity": "local-owner-selector"},
        "mode": "unary",
        "mutates": True,
        "retry": "never",
        "confirmation": "required",
        "audience": ["local-operator", "local-owner"],
        "paramsSchema": {"description": "café"},
        "resultSchema": {"type": "object"},
        "errors": [],
        "ipcParamValidation": {},
        "declared": True,
        "state": "available",
        "callable": True,
        "reason": None,
    },
]




def test_declaration_digest_matches_the_http_projection_byte_for_byte() -> None:
    daemon_document = declaration_document(_GOLDEN)
    http_document = http_declaration_document(_GOLDEN)
    assert daemon_document == http_document
    assert len(declaration_digest()) == 7 + 64
    assert declaration_digest().startswith("sha256:")
    assert (
        canonical_digest(daemon_document)
        == http_declaration_digest(_GOLDEN)
    )
    assert "café" in str(daemon_document)


def test_declaration_digest_ignores_dynamic_state_and_rejects_shape_drift() -> None:
    catalog = export_catalog(METHOD_REGISTRY.specs)
    with_dynamic = {
        **catalog,
        "methods": [{**entry, **_DYNAMIC} for entry in catalog["methods"]],
    }
    assert catalog_result_digest(with_dynamic) == declaration_digest()

    changed = {
        **with_dynamic,
        "methods": with_dynamic["methods"][:-1],
    }
    assert catalog_result_digest(changed) != declaration_digest()
    assert (
        catalog_result_digest({"schemaVersion": 2, "methods": []})
        is None
    )
    assert catalog_result_digest({"schemaVersion": 1, "methods": []}) != declaration_digest()
    assert catalog_result_digest({"methods": []}) is None
    assert catalog_result_digest(None) is None


def test_phase_caller_spec_enforces_explicit_pairs() -> None:
    serving_only = SimpleNamespace(phase_caller=BOOTSTRAP_AND_SERVING)
    bootstrap_only = SimpleNamespace(phase_caller=BOOTSTRAP_ONLY)
    unconstrained = SimpleNamespace(phase_caller=None)

    assert spec_phase_caller_allows(
        serving_only, "bootstrap", "local-bootstrap"
    )
    assert spec_phase_caller_allows(serving_only, "serving", "local-gui")
    assert not spec_phase_caller_allows(
        serving_only, "serving", "local-bootstrap"
    )
    assert not spec_phase_caller_allows(
        serving_only, "bootstrap", "local-gui"
    )
    assert spec_phase_caller_allows(
        bootstrap_only, "bootstrap", "local-bootstrap"
    )
    assert not spec_phase_caller_allows(
        bootstrap_only, "serving", "local-gui"
    )
    assert spec_phase_caller_allows(unconstrained, "bootstrap", "local-gui")


def test_production_registry_is_consulted_for_registered_methods() -> None:
    assert phase_caller_allows("ping", "serving", "local-gui")
    assert phase_caller_allows("not-registered", "bootstrap", "local-gui")
