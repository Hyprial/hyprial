"""A-03 method declarations: completeness, binding and negative controls."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.request_catalog import agent as agent_catalog
from hyprial.daemon.impl.request_catalog.registry import (
    METHOD_REGISTRY,
    MIGRATED_FAMILIES,
    MethodRegistry,
)
from hyprial.kernel import (
    DaemonRequestError,
    HttpBinding,
    IdentityBinding,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    MethodSpecError,
    PreviewSpec,
    StreamSpec,
    export_catalog,
    export_openapi,
    ipc_errors,
    validate_method_spec,
    validate_method_specs,
)


def _spec(**overrides: Any) -> MethodSpec:
    values: dict[str, Any] = {
        "name": "probe.read",
        "owner": "daemon.probe",
        "params_schema": {
            "type": "object",
            "properties": {"from": {"type": "string", "minLength": 1}},
            "required": ["from"],
            "additionalProperties": False,
        },
        "result_schema": {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        "errors": (
            MethodError(
                ipc_errors.INVALID_ARGUMENT,
                {
                    "type": "object",
                    "properties": {"actor": {"type": "string"}},
                    "additionalProperties": False,
                },
            ),
        ),
        "audience": ("local-operator",),
        "idempotency": IdempotencySpec(mode="safe", scope="read-only-query"),
    }
    values.update(overrides)
    return MethodSpec(**values)


def _preview_specs() -> tuple[MethodSpec, MethodSpec]:
    preview = _spec(
        name="probe.remove.preview",
        confirmation="preview",
        preview=PreviewSpec(ttl_seconds=300),
        result_schema={
            "type": "object",
            "properties": {
                "operationId": {"type": "string", "minLength": 1},
                "expiresAt": {"type": "string", "minLength": 1},
                "summary": {"type": "string", "minLength": 1},
            },
            "required": ["operationId", "expiresAt", "summary"],
            "additionalProperties": False,
        },
    )
    execute = _spec(
        name="probe.remove",
        mutates=True,
        retry="never",
        idempotency=IdempotencySpec(mode="never", scope="probe-remove"),
        confirmation="execute",
        preview_of="probe.remove.preview",
        params_schema={
            "type": "object",
            "properties": {"operationId": {"type": "string", "minLength": 1}},
            "required": ["operationId"],
            "additionalProperties": False,
        },
        result_schema={
            "type": "object",
            "properties": {"ok": {"const": True}, "removed": {"type": "boolean"}},
            "required": ["ok", "removed"],
            "additionalProperties": False,
        },
    )
    return preview, execute


def test_every_declared_method_is_declared_and_bound_one_to_one() -> None:
    # The catalog grows method by method; this pins that every declared name
    # carries one spec and one handler, never a family-wide claim.
    assert {
        "link.add",
        "link.drop",
        "link.list",
        "link.stats",
        "session.turn.refresh",
    } <= set(METHOD_REGISTRY.names)
    for spec in METHOD_REGISTRY.specs:
        validate_method_spec(spec)
        assert spec.errors, spec.name
        assert spec.params_schema and spec.result_schema
        if spec.mutates:
            assert spec.retry in {"never", "idempotency-key"}
            if spec.retry == "idempotency-key":
                assert spec.idempotency.key_field
        else:
            assert spec.retry == "safe"


def test_new_methods_can_be_declared_without_migrating_their_whole_family() -> None:
    assert "message.send.lookup" in METHOD_REGISTRY
    assert MIGRATED_FAMILIES == {"daemon", "link"}
    assert "message.send" not in METHOD_REGISTRY


def test_every_declared_name_belongs_to_an_explicit_migrated_family() -> None:
    families = {name.split(".", 1)[0] for name in METHOD_REGISTRY.names}
    assert families - MIGRATED_FAMILIES == {"agent", "message", "session"}
    assert "link" in MIGRATED_FAMILIES
    assert "agent" not in MIGRATED_FAMILIES
    assert {
        name for name in METHOD_REGISTRY.names if name.startswith("agent.")
    } == set(agent_catalog.PARTIAL_MIGRATED_METHODS) | {
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
    }


def test_every_declared_name_is_served_before_any_legacy_fallback() -> None:
    """A declared name may live in a still-legacy family; it never falls back.

    ``link`` is fully migrated (no legacy chain at all). ``session`` is not: its
    boundary reports stay on the frozen legacy chain, so only the declared
    ``session.turn.refresh`` is served here, before that chain, and the
    legacy-method ledger still forbids the chain from growing.
    """

    families = {name.split(".", 1)[0] for name in METHOD_REGISTRY.names}
    assert MIGRATED_FAMILIES == {"daemon", "link"}
    assert families == {"daemon", "link", "message", "agent", "session"}
    assert "session.turn.refresh" in METHOD_REGISTRY


def test_read_exposure_is_explicit() -> None:
    exposed = {
        spec.name for spec in METHOD_REGISTRY.specs if spec.http_exposed
    }
    assert exposed == {
        "agent.list",
        "agent.provider.default",
        "agent.secret.list",
        "link.list",
        "link.stats",
    }


def test_empty_or_boolean_schemas_are_rejected() -> None:
    with pytest.raises(MethodSpecError, match="empty schema"):
        validate_method_spec(_spec(params_schema={}))
    with pytest.raises(MethodSpecError, match="boolean schema"):
        validate_method_spec(_spec(result_schema=True))
    with pytest.raises(MethodSpecError, match="additionalProperties"):
        validate_method_spec(
            _spec(
                result_schema={
                    "type": "object",
                    "properties": {"ok": {"const": True}},
                }
            )
        )


def test_a_malformed_error_data_schema_is_rejected() -> None:
    with pytest.raises(MethodSpecError):
        validate_method_spec(
            _spec(errors=(MethodError(ipc_errors.INVALID_ARGUMENT, {}),))
        )
    with pytest.raises(MethodSpecError, match="duplicate error code"):
        validate_method_spec(
            _spec(
                errors=(
                    MethodError(ipc_errors.INVALID_ARGUMENT),
                    MethodError(ipc_errors.INVALID_ARGUMENT),
                )
            )
        )


def test_missing_and_duplicate_handlers_are_rejected() -> None:
    registry = MethodRegistry()
    with pytest.raises(TypeError, match="handler"):
        registry.bind(_spec(), None)  # type: ignore[arg-type]
    registry.bind(_spec(), lambda _app, _params, **_kwargs: {"ok": True})
    with pytest.raises(ValueError, match="duplicate"):
        registry.bind(_spec(), lambda _app, _params, **_kwargs: {"ok": True})
    with pytest.raises(ValueError, match="duplicate method name"):
        validate_method_specs((_spec(), _spec()))


def test_unregistered_method_is_a_hard_failure() -> None:
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(object(), "link.unknown", {"from": "agent:a:b:c"})
    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND


def test_runtime_params_and_result_are_checked_against_the_declaration() -> None:
    registry = MethodRegistry()
    registry.bind(_spec(), lambda _app, _params, **_kwargs: {"ok": "not-a-bool"})
    registry.freeze()

    with pytest.raises(DaemonRequestError) as bad_params:
        registry.invoke(object(), "probe.read", {"from": ""})
    assert bad_params.value.code == ipc_errors.INVALID_ARGUMENT

    with pytest.raises(DaemonRequestError) as bad_result:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert bad_result.value.code == ipc_errors.DAEMON_ERROR


def test_preview_and_execute_pairing_is_validated() -> None:
    preview, execute = _preview_specs()
    assert validate_method_specs((preview, execute)) == (preview, execute)

    with pytest.raises(MethodSpecError, match="only operationId"):
        validate_method_spec(
            _spec(
                name="probe.remove",
                mutates=True,
                retry="never",
                idempotency=IdempotencySpec(mode="never"),
                confirmation="execute",
                preview_of="probe.remove.preview",
                params_schema={
                    "type": "object",
                    "properties": {
                        "operationId": {"type": "string"},
                        "from": {"type": "string"},
                    },
                    "required": ["operationId"],
                    "additionalProperties": False,
                },
            )
        )
    with pytest.raises(MethodSpecError, match="does not declare"):
        validate_method_spec(
            _spec(
                name="probe.remove.preview",
                confirmation="preview",
                preview=PreviewSpec(),
            )
        )
    with pytest.raises(MethodSpecError, match="preview_of must name"):
        validate_method_specs((execute,))


def test_declared_schemas_are_detached_and_frozen() -> None:
    params = {
        "type": "object",
        "properties": {"from": {"type": "string", "minLength": 1}},
        "required": ["from"],
        "additionalProperties": False,
    }
    spec = _spec(params_schema=params)
    params["properties"]["from"]["minLength"] = 99
    assert spec.params_schema["properties"]["from"]["minLength"] == 1
    with pytest.raises(TypeError):
        spec.params_schema["type"] = "array"  # type: ignore[index]
    with pytest.raises(TypeError):
        spec.params_schema["properties"]["from"]["minLength"] = 2  # type: ignore[index]

    exposed = replace(
        spec,
        http_exposed=True,
        http_binding=HttpBinding(
            IdentityBinding("local-operator-selector", "from"),
            readiness="link-service",
        ),
    )
    document = export_openapi((exposed,))
    document["paths"].clear()
    assert export_openapi((exposed,))["paths"]


def test_non_finite_numbers_and_external_refs_are_rejected() -> None:
    with pytest.raises(MethodSpecError, match="non-finite"):
        validate_method_spec(
            _spec(
                result_schema={
                    "type": "object",
                    "properties": {"ok": {"const": True}, "n": {"type": "number", "maximum": float("nan")}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            )
        )
    with pytest.raises(MethodSpecError, match="external refs"):
        validate_method_spec(
            _spec(
                result_schema={
                    "type": "object",
                    "properties": {"ok": {"$ref": "https://example.invalid/schema.json"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            )
        )


def test_local_ref_and_format_checker_are_enforced() -> None:
    spec = _spec(
        params_schema={
            "type": "object",
            "properties": {
                "from": {"$ref": "#/$defs/actor"},
                "contact": {"type": "string", "format": "email"},
            },
            "required": ["from", "contact"],
            "$defs": {"actor": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        }
    )
    registry = MethodRegistry()
    registry.bind(spec, lambda _app, _params, **_kwargs: {"ok": True})
    registry.freeze()

    with pytest.raises(DaemonRequestError) as bad_format:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c", "contact": "nope"})
    assert bad_format.value.code == ipc_errors.INVALID_ARGUMENT
    registry.invoke(object(), "probe.read", {"from": "agent:a:b:c", "contact": "a@b.co"})


def test_existing_method_name_syntax_is_accepted() -> None:
    from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS

    for name in sorted(_IPC_STATS_METHODS):
        validate_method_spec(_spec(name=name))
    validate_method_spec(_spec(name="ping"))
    validate_method_spec(_spec(name="hosts"))


def test_contract_failures_do_not_echo_values_or_undeclared_errors() -> None:
    registry = MethodRegistry()
    registry.bind(_spec(), lambda _app, _params, **_kwargs: {"ok": False, "secret": "s3cr3t"})
    registry.freeze()
    with pytest.raises(DaemonRequestError) as bad_result:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert bad_result.value.code == ipc_errors.DAEMON_ERROR
    assert "s3cr3t" not in str(bad_result.value)
    assert "False" not in str(bad_result.value)

    def undeclared_error(_app: Any, _params: Any, **_kwargs: Any) -> Any:
        raise DaemonRequestError(ipc_errors.LINK_UNKNOWN, "boom")

    registry = MethodRegistry()
    registry.bind(_spec(), undeclared_error)
    registry.freeze()
    with pytest.raises(DaemonRequestError) as undeclared:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert undeclared.value.code == ipc_errors.DAEMON_ERROR

    def undeclared_data(_app: Any, _params: Any, **_kwargs: Any) -> Any:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "boom", {"secret": "s3cr3t"})

    registry = MethodRegistry()
    registry.bind(_spec(), undeclared_data)
    registry.freeze()
    with pytest.raises(DaemonRequestError) as bad_data:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert bad_data.value.code == ipc_errors.DAEMON_ERROR
    assert "s3cr3t" not in str(bad_data.value)

    def declared_error(_app: Any, _params: Any, **_kwargs: Any) -> Any:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "boom", {"actor": "a"})

    registry = MethodRegistry()
    registry.bind(_spec(), declared_error)
    registry.freeze()
    with pytest.raises(DaemonRequestError) as declared:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert declared.value.code == ipc_errors.INVALID_ARGUMENT
    assert declared.value.data == {"actor": "a"}


@pytest.mark.parametrize(
    "location",
    ("not", "if", "dependentSchemas"),
)
def test_external_refs_in_nested_schema_keywords_are_rejected(location: str) -> None:
    nested: dict[str, Any] = {"$ref": "https://offline-probe.invalid/schema"}
    if location == "not":
        schema = {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
            "not": nested,
        }
    elif location == "if":
        schema = {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
            "if": nested,
            "then": {"type": "object"},
        }
    else:
        schema = {
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
            "dependentSchemas": {"ok": nested},
        }
    with pytest.raises(MethodSpecError, match="external refs"):
        validate_method_spec(_spec(result_schema=schema))


def test_dynamic_ref_and_open_additional_properties_are_rejected() -> None:
    with pytest.raises(MethodSpecError, match="unsupported"):
        validate_method_spec(
            _spec(
                result_schema={
                    "type": "object",
                    "properties": {"ok": {"$dynamicRef": "#thing"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                }
            )
        )
    with pytest.raises(MethodSpecError, match="arbitrary data"):
        validate_method_spec(
            _spec(
                result_schema={
                    "type": "object",
                    "properties": {"ok": {"const": True}},
                    "required": ["ok"],
                    "additionalProperties": True,
                }
            )
        )


def test_error_data_uses_the_declared_format_checker() -> None:
    spec = _spec(
        errors=(
            MethodError(
                ipc_errors.INVALID_ARGUMENT,
                {
                    "type": "object",
                    "properties": {"contact": {"type": "string", "format": "email"}},
                    "additionalProperties": False,
                },
            ),
        )
    )

    def bad_data(_app: Any, _params: Any, **_kwargs: Any) -> Any:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "boom", {"contact": "nope"})

    registry = MethodRegistry()
    registry.bind(spec, bad_data)
    registry.freeze()
    with pytest.raises(DaemonRequestError) as caught:
        registry.invoke(object(), "probe.read", {"from": "agent:a:b:c"})
    assert caught.value.code == ipc_errors.DAEMON_ERROR
    assert "nope" not in str(caught.value)


def test_errors_audience_and_nested_metadata_are_frozen() -> None:
    from hyprial.daemon.impl.request_catalog import LINK_STATS_SPEC

    errors = [MethodError(ipc_errors.INVALID_ARGUMENT)]
    audience = ["local-operator"]
    terminal_states = ["done"]
    binds = ["caller", "method", "paramsDigest", "expiry"]
    stream = StreamSpec(terminal_states=terminal_states)
    preview = PreviewSpec(binds=binds)
    spec = replace(
        _spec(errors=tuple(errors), audience=tuple(audience)),
        errors=errors,
        audience=audience,
    )
    errors.append(MethodError(ipc_errors.LINK_UNKNOWN))
    audience.append("adapter")
    terminal_states.append("failed")
    binds.append("extra")
    assert len(spec.errors) == 1
    assert spec.audience == ("local-operator",)
    assert stream.terminal_states == ("done",)
    assert preview.binds == ("caller", "method", "paramsDigest", "expiry")

    registry = MethodRegistry()
    registry.bind(replace(LINK_STATS_SPEC, audience=audience), lambda *_args, **_kwargs: {"ok": True})
    registry.freeze()
    assert registry.specs[0].audience == ("local-operator", "adapter")


def test_boolean_and_enum_metadata_reject_string_or_invalid_values() -> None:
    from hyprial.daemon.impl.request_catalog import LINK_STATS_SPEC

    with pytest.raises(MethodSpecError, match="http_exposed"):
        validate_method_spec(replace(LINK_STATS_SPEC, http_exposed="false"))
    with pytest.raises(MethodSpecError, match="mutates"):
        validate_method_spec(replace(LINK_STATS_SPEC, mutates="false"))
    with pytest.raises(MethodSpecError, match="confirmation"):
        validate_method_spec(replace(LINK_STATS_SPEC, confirmation="maybe"))
    with pytest.raises(MethodSpecError, match="max_batch"):
        StreamSpec(max_batch=True)
    with pytest.raises(MethodSpecError, match="ttl_seconds"):
        PreviewSpec(ttl_seconds=True)


def test_pure_catalog_import_does_not_load_composition_or_uvicorn() -> None:
    root = Path(__file__).resolve().parents[4]
    script = (
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "import hyprial\n"
        "from hyprial.daemon import daemon_catalog, daemon_openapi\n"
        "root = Path(os.environ['M03_REPO_ROOT']).resolve()\n"
        "assert Path(hyprial.__file__).resolve().is_relative_to(root / 'src'), hyprial.__file__\n"
        "catalog = daemon_catalog()\n"
        "names = {entry['name'] for entry in catalog['methods']}\n"
        "expected = {\n"
        "    'agent.list', 'agent.secret.list', 'agent.provider.default',\n"
        "    'agent.provider.login', 'agent.provider.logout',\n"
        "}\n"
        "assert expected <= names, expected - names\n"
        "daemon_openapi()\n"
        "loaded = [name for name in (\n"
        "    'uvicorn', 'mcp', 'hyprial.daemon.impl.composition'\n"
        ") if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    env = dict(os.environ)
    source = str(root / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source + (os.pathsep + current if current else "")
    env["M03_REPO_ROOT"] = str(root)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_http_exposed_without_binding_is_declaration_eligibility() -> None:
    """A declaration may be eligible for HTTP before a shared binding exists."""

    spec = _spec(
        name="synthetic.eligible",
        owner="synthetic.probe",
        http_exposed=True,
    )
    validate_method_spec(spec)

    document = export_openapi((spec,))
    post = document["paths"]["/daemon/request/synthetic.eligible"]["post"]
    default_binding = {
        "identity": {"mode": "none", "param": None},
        "readiness": None,
    }
    assert post["x-hyprial-http-binding"] == default_binding
    entry = export_catalog((spec,))["methods"][0]
    assert entry["httpExposed"] is True
    assert entry["httpBinding"] == default_binding
    assert spec.effective_http_binding == HttpBinding(IdentityBinding("none"))

    # Registry assembly must accept the eligibility declaration; runtime
    # routing filters it separately instead of failing the whole registry.
    registry = MethodRegistry.from_bindings(
        ((spec, lambda app, params, **_: {"ok": True}),)
    )
    assert registry.names == ("synthetic.eligible",)


def test_http_binding_is_explicit_and_fails_closed() -> None:
    binding = HttpBinding(IdentityBinding("local-operator-selector", "from"))

    with pytest.raises(MethodSpecError, match="requires http_exposed"):
        validate_method_spec(_spec(http_binding=binding))

    with pytest.raises(MethodSpecError, match="unsupported HTTP identity mode"):
        validate_method_spec(
            _spec(
                http_exposed=True,
                http_binding=HttpBinding(
                    IdentityBinding("bound-agent-session", "from")  # type: ignore[arg-type]
                ),
            )
        )

    with pytest.raises(MethodSpecError, match="cannot name a selector"):
        validate_method_spec(
            _spec(
                http_exposed=True,
                http_binding=HttpBinding(IdentityBinding("none", "from")),
            )
        )

    with pytest.raises(MethodSpecError, match="is not a declared parameter"):
        validate_method_spec(
            _spec(
                http_exposed=True,
                http_binding=HttpBinding(IdentityBinding("local-operator-selector", "actor")),
            )
        )

    with pytest.raises(MethodSpecError, match="must accept a string"):
        validate_method_spec(
            _spec(
                params_schema={
                    "type": "object",
                    "properties": {"from": {"type": "integer"}},
                    "required": ["from"],
                    "additionalProperties": False,
                },
                http_exposed=True,
                http_binding=HttpBinding(IdentityBinding("local-operator-selector", "from")),
            )
        )


def test_identity_none_is_legal_and_exported_as_null_for_unexposed() -> None:
    no_identity = _spec(
        params_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        http_exposed=True,
        http_binding=HttpBinding(IdentityBinding("none")),
    )
    validate_method_spec(no_identity)
    document = export_openapi((no_identity,))
    assert document["paths"]["/daemon/request/probe.read"]["post"][
        "x-hyprial-http-binding"
    ] == {"identity": {"mode": "none", "param": None}, "readiness": None}
    entry = export_openapi((_spec(),))["x-hyprial-methods"][0]
    assert entry["httpBinding"] is None


def test_registry_refuses_an_unassembled_readiness_probe() -> None:
    spec = _spec(
        http_exposed=True,
        http_binding=HttpBinding(
            IdentityBinding("local-operator-selector", "from"),
            readiness="not-a-probe",
        ),
    )
    with pytest.raises(MethodSpecError, match="is not assembled"):
        MethodRegistry.from_bindings(((spec, lambda app, params, **_: {"ok": True}),))
