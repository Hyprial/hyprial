"""Pure daemon request method declarations and deterministic OpenAPI projection.

The declarations are data: importing this module never starts a daemon, reads a
home, resolves an identity or opens a socket.  A method's params/result/error
schemas, streaming, idempotency and preview pairing are validated here, and the
same objects feed the offline OpenAPI 3.1 export.  The export is a contract
projection only; it does not start an HTTP gateway.

Legacy IPC compatibility is explicit: ``ipc_param_validation="handler"`` keeps
the declared schema as the strict HTTP/client contract while delegating raw IPC
parameter admission and its established error order to the bound handler.
"""

from __future__ import annotations

import functools
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .http_params import project_http_params_schema
from .examples.echo import freeze_mock_echo, validate_mock_echo

from .examples import (
    MethodExampleSet,
    example_to_json,
    validate_method_example_sets,
)
from .legacy_error_codes import is_declared_error_code

__all__ = [
    "Confirmation",
    "HttpBinding",
    "HttpIdentityMode",
    "IdentityBinding",
    "IdempotencySpec",
    "MethodError",
    "MethodSpec",
    "MethodSpecError",
    "PhaseCallerSpec",
    "PreviewSpec",
    "RetryMode",
    "StreamSpec",
    "export_catalog",
    "export_openapi",
    "validate_method_spec",
    "validate_method_specs",
]

MethodMode = Literal["unary", "cursor-poll"]
Confirmation = Literal["none", "preview", "execute"]
RetryMode = Literal["safe", "idempotency-key", "never"]
HttpIdentityMode = Literal[
    "none",
    "local-owner-selector",
    "local-owner-user-selector",
]

_NAME = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
_OWNER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
AUDIENCES = frozenset(
    {
        "local-operator",
        "bound-agent",
        "adapter",
        "internal",
        "peer-node",
    }
)
METHOD_PHASES = frozenset({"bootstrap", "serving"})
#: ``local-operator`` is the daemon's own local operator surface: the CLI (and
#: anything else on the daemon request socket) reaches a serving method through
#: it.  It is still an exact (phase, caller) pair, never a wildcard.
METHOD_CALLERS = frozenset({"local-bootstrap", "local-gui", "local-operator"})
_PREVIEW_BINDS = frozenset({"caller", "method", "paramsDigest", "expiry"})
_PREVIEW_TTL_MAX_SECONDS = 3600
_SCHEMA_COMBINATORS = ("$ref", "oneOf", "anyOf", "allOf", "const", "enum")


class MethodSpecError(ValueError):
    """A declaration cannot be consumed by the registry or the export."""


def _freeze_json(value: Any, where: str) -> Any:
    """Detach and freeze declaration data so callers cannot mutate a catalog.

    Mappings become read-only proxies and arrays become tuples.  NaN/Infinity
    are not JSON data and are rejected here, before any export can emit them.
    """

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MethodSpecError(f"{where}: non-finite JSON number is not allowed")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MethodSpecError(f"{where}: schema keys must be strings")
            frozen[key] = _freeze_json(item, f"{where}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, f"{where}[{index}]")
            for index, item in enumerate(value)
        )
    raise MethodSpecError(
        f"{where}: {type(value).__name__} is not JSON-serialisable declaration data"
    )


def _thaw(value: Any) -> Any:
    """Return a detached plain-JSON copy of frozen declaration data."""

    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MethodError:
    """One domain error the method may answer with."""

    code: str
    data_schema: Mapping[str, Any] | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.data_schema is not None:
            object.__setattr__(
                self,
                "data_schema",
                _freeze_json(self.data_schema, f"error {self.code} data"),
            )


@dataclass(frozen=True, slots=True)
class PhaseCallerSpec:
    """The exact (phase, caller kind) pairs allowed to invoke a method."""

    pairs: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        normalized: list[tuple[str, str]] = []
        for pair in self.pairs:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise MethodSpecError(
                    "phaseCaller.pairs must contain (phase, caller) pairs"
                )
            phase, caller = pair
            if not isinstance(phase, str) or not phase:
                raise MethodSpecError("phaseCaller phase must be a non-empty string")
            if not isinstance(caller, str) or not caller:
                raise MethodSpecError("phaseCaller caller must be a non-empty string")
            if phase not in METHOD_PHASES:
                raise MethodSpecError(f"phaseCaller has unknown phase {phase!r}")
            if caller not in METHOD_CALLERS:
                raise MethodSpecError(f"phaseCaller has unknown caller {caller!r}")
            normalized.append((phase, caller))
        if not normalized:
            raise MethodSpecError("phaseCaller.pairs must not be empty")
        if len(set(normalized)) != len(normalized):
            raise MethodSpecError("phaseCaller.pairs must be unique")
        object.__setattr__(self, "pairs", tuple(normalized))

    def allows(self, phase: str, caller: str) -> bool:
        """Return whether this exact phase/caller pair is permitted."""

        return (phase, caller) in self.pairs


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """Logical cursor-poll metadata; not a second transport."""

    cursor_field: str = "nextCursor"
    max_batch: int = 1
    wait_field: str | None = None
    terminal_states: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminal_states", tuple(self.terminal_states))
        if not isinstance(self.cursor_field, str) or not self.cursor_field:
            raise MethodSpecError("stream cursor_field must be a non-empty string")
        if type(self.max_batch) is not int or self.max_batch < 1:
            raise MethodSpecError("stream max_batch must be a positive integer")
        if self.wait_field is not None and (
            not isinstance(self.wait_field, str) or not self.wait_field
        ):
            raise MethodSpecError("stream wait_field must be a non-empty string or None")
        if not all(isinstance(item, str) and item for item in self.terminal_states):
            raise MethodSpecError("stream terminal_states must be non-empty strings")


@dataclass(frozen=True, slots=True)
class IdempotencySpec:
    """Retry semantics declared before any handler runs."""

    mode: RetryMode
    key_field: str | None = None
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class PreviewSpec:
    """The preview half of a preview→execute confirmation pair."""

    operation_field: str = "operationId"
    expires_field: str = "expiresAt"
    summary_field: str = "summary"
    ttl_seconds: int = 300
    binds: tuple[str, ...] = ("caller", "method", "paramsDigest", "expiry")

    def __post_init__(self) -> None:
        object.__setattr__(self, "binds", tuple(self.binds))
        for name in ("operation_field", "expires_field", "summary_field"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise MethodSpecError(f"preview {name} must be a non-empty string")
        if type(self.ttl_seconds) is not int:
            raise MethodSpecError("preview ttl_seconds must be an integer")


@dataclass(frozen=True, slots=True)
class IdentityBinding:
    """How one HTTP request supplies the caller selector, if it has one."""

    mode: HttpIdentityMode
    param: str | None = None


@dataclass(frozen=True, slots=True)
class HttpBinding:
    """Explicit HTTP transport binding; never inferred from a parameter name."""

    identity: IdentityBinding
    readiness: str | None = None
    #: Optional second identity parameter the trusted HTTP host injects.  Link
    #: administration needs the operator's canonical agent as the subject while
    #: the primary selector remains the local owner; direct HTTP callers may
    #: never supply this field.
    on_behalf_of: str | None = None
    #: HTTP-only narrowing, owned by this MethodSpec. Never replaces IPC params.
    params_constraints: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.params_constraints is not None:
            object.__setattr__(self, "params_constraints", _freeze_json(
                self.params_constraints, "http_binding.params_constraints",
            ))


_DEFAULT_HTTP_BINDING = HttpBinding(IdentityBinding("none"))


@dataclass(frozen=True, slots=True)
class MethodSpec:
    """Immutable declaration of exactly one daemon request method."""

    name: str
    owner: str
    params_schema: Mapping[str, Any]
    result_schema: Mapping[str, Any]
    errors: tuple[MethodError, ...]
    mode: MethodMode = "unary"
    mutates: bool = False
    retry: RetryMode = "safe"
    audience: tuple[str, ...] = ("local-operator",)
    http_exposed: bool = False
    http_binding: HttpBinding | None = None
    confirmation: Confirmation = "none"
    stream: StreamSpec | None = None
    idempotency: IdempotencySpec | None = None
    preview: PreviewSpec | None = None
    preview_of: str | None = None
    ipc_param_validation: Literal["schema", "handler"] = "schema"
    phase_caller: PhaseCallerSpec | None = None
    mock_echo: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "audience", tuple(self.audience))
        object.__setattr__(
            self, "params_schema", _freeze_json(self.params_schema, f"{self.name}.params")
        )
        object.__setattr__(
            self, "result_schema", _freeze_json(self.result_schema, f"{self.name}.result")
        )
        object.__setattr__(self, "mock_echo", freeze_mock_echo(self.mock_echo, self.name))

    @property
    def effective_http_binding(self) -> HttpBinding | None:
        """The explicit binding, or the no-selector default for eligibility.

        ``http_exposed=True`` without an explicit binding declares eligibility
        without a selector.  The generic projection gives that declaration the
        local-operator no-selector binding; it never infers a caller parameter.
        Runtime routing still applies the audience/unary/safe profile filters.
        """

        if self.http_binding is not None:
            return self.http_binding
        if self.http_exposed:
            return _DEFAULT_HTTP_BINDING
        return None


def _json_safe(value: Any, where: str) -> Any:
    """Copy one declaration tree and reject values JSON cannot represent."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MethodSpecError(f"{where}: non-finite JSON number is not allowed")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise MethodSpecError(f"{where}: schema keys must be strings")
            copied[key] = _json_safe(item, f"{where}.{key}")
        return copied
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, f"{where}[{index}]") for index, item in enumerate(value)]
    raise MethodSpecError(
        f"{where}: {type(value).__name__} is not JSON-serialisable declaration data"
    )


def _schema_problems(schema: Any, where: str) -> list[str]:
    if isinstance(schema, bool):
        return [f"{where}: boolean schemas are not allowed"]
    if not isinstance(schema, Mapping):
        return [f"{where}: schema must be an object, got {type(schema).__name__}"]
    if not schema:
        return [f"{where}: empty schema is not allowed"]
    problems: list[str] = []
    if not any(key in schema for key in ("type", *_SCHEMA_COMBINATORS)):
        problems.append(f"{where}: schema needs a type or a combinator/$ref")
    if "type" in schema and not isinstance(schema["type"], (str, list)):
        problems.append(f"{where}: type must be a string or a list of strings")
    for key in ("$ref", "$dynamicRef", "$recursiveRef"):
        reference = schema.get(key)
        if reference is None:
            continue
        if key != "$ref":
            problems.append(f"{where}: {key} is unsupported; use local $ref only")
        elif not isinstance(reference, str) or not reference.startswith("#"):
            problems.append(
                f"{where}: only local #/... $ref is allowed; external refs are offline-forbidden"
            )
    for key in ("properties", "patternProperties", "$defs"):
        children = schema.get(key)
        if children is None:
            continue
        if not isinstance(children, Mapping):
            problems.append(f"{where}.{key}: must be an object")
            continue
        for name, child in children.items():
            problems.extend(_schema_problems(child, f"{where}.{key}.{name}"))
    if "additionalProperties" in schema:
        child = schema["additionalProperties"]
        if child is False:
            pass
        elif child is True:
            problems.append(f"{where}.additionalProperties: true would accept arbitrary data")
        elif isinstance(child, Mapping):
            problems.extend(_schema_problems(child, f"{where}.additionalProperties"))
        else:
            problems.append(f"{where}.additionalProperties: must be a schema or boolean")
    if "items" in schema:
        problems.extend(_schema_problems(schema["items"], f"{where}.items"))
    for key in (
        "not",
        "if",
        "then",
        "else",
        "contains",
        "propertyNames",
        "contentSchema",
    ):
        if key in schema:
            problems.extend(_schema_problems(schema[key], f"{where}.{key}"))
    for key in ("unevaluatedProperties", "unevaluatedItems", "additionalItems"):
        if key not in schema:
            continue
        child = schema[key]
        if child is True:
            problems.append(f"{where}.{key}: true would accept arbitrary data")
        elif child is False:
            pass
        elif isinstance(child, Mapping):
            problems.extend(_schema_problems(child, f"{where}.{key}"))
        else:
            problems.append(f"{where}.{key}: must be a schema or boolean")
    for key in ("dependentSchemas", "definitions"):
        children = schema.get(key)
        if children is None:
            continue
        if not isinstance(children, Mapping):
            problems.append(f"{where}.{key}: must be an object")
            continue
        for name, child in children.items():
            problems.extend(_schema_problems(child, f"{where}.{key}.{name}"))
    for key in ("oneOf", "anyOf", "allOf", "prefixItems"):
        children = schema.get(key)
        if children is None:
            continue
        if not isinstance(children, list):
            problems.append(f"{where}.{key}: must be a list")
            continue
        for index, child in enumerate(children):
            problems.extend(_schema_problems(child, f"{where}.{key}[{index}]"))
    return problems


def _reference_problems(value: Any, where: str) -> list[str]:
    """Find refs anywhere in the declaration tree, including unknown keywords."""

    problems: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key != "$ref":
                    problems.append(f"{where}.{key}: unsupported reference keyword")
                elif not isinstance(item, str) or not item.startswith("#"):
                    problems.append(
                        f"{where}.{key}: only local #/... $ref is allowed; external refs are offline-forbidden"
                    )
            problems.extend(_reference_problems(item, f"{where}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problems.extend(_reference_problems(item, f"{where}[{index}]"))
    return problems


def _validate_schema(
    schema: Mapping[str, Any], label: str, *, object_root: bool, metaschema: bool = True
) -> None:
    declared = _json_safe(schema, label)
    problems = _reference_problems(declared, label)
    problems.extend(_schema_problems(declared, label))
    if object_root and isinstance(declared, Mapping):
        if declared.get("type") != "object":
            problems.append(f"{label}: root schema must be type object")
        if not isinstance(declared.get("properties"), Mapping):
            problems.append(f"{label}: object root must declare properties")
        if "additionalProperties" not in declared:
            problems.append(
                f"{label}: object root must declare additionalProperties explicitly"
            )
    if metaschema and isinstance(declared, Mapping):
        message = _metaschema_error(json.dumps(declared, sort_keys=True))
        if message is not None:
            problems.append(f"{label}: {message}")
    if problems:
        raise MethodSpecError("; ".join(problems))


@functools.lru_cache(maxsize=4096)
def _metaschema_error(canonical: str) -> str | None:
    """Draft 2020-12 metaschema verdict for one JSON-safe schema, by content."""

    try:
        Draft202012Validator.check_schema(json.loads(canonical))
    except SchemaError as error:
        return error.message
    return None


def _required_properties(schema: Mapping[str, Any], where: str) -> tuple[Mapping[str, Any], set[str]]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        raise MethodSpecError(f"{where}: preview result needs object properties")
    required = schema.get("required", [])
    if not isinstance(required, (list, tuple)) or not all(
        isinstance(item, str) for item in required
    ):
        raise MethodSpecError(f"{where}: required must be a list of strings")
    return properties, set(required)


def _validate_preview(spec: MethodSpec) -> None:
    preview = spec.preview
    if spec.confirmation == "none":
        if preview is not None or spec.preview_of is not None:
            raise MethodSpecError(f"{spec.name}: confirmation none cannot declare a preview")
        return
    if spec.confirmation == "preview":
        if preview is None or spec.preview_of is not None:
            raise MethodSpecError(f"{spec.name}: preview method needs exactly one preview block")
        if not 1 <= preview.ttl_seconds <= _PREVIEW_TTL_MAX_SECONDS:
            raise MethodSpecError(
                f"{spec.name}: preview ttl_seconds must be 1..{_PREVIEW_TTL_MAX_SECONDS}"
            )
        if set(preview.binds) != _PREVIEW_BINDS:
            raise MethodSpecError(
                f"{spec.name}: preview must bind caller, method, paramsDigest and expiry"
            )
        properties, required = _required_properties(
            spec.result_schema, f"{spec.name}.result_schema"
        )
        for field in (preview.operation_field, preview.expires_field, preview.summary_field):
            if field not in properties:
                raise MethodSpecError(
                    f"{spec.name}: preview result does not declare {field!r}"
                )
        for field in (preview.operation_field, preview.expires_field):
            if field not in required:
                raise MethodSpecError(
                    f"{spec.name}: preview result must require {field!r}"
                )
        return
    if spec.preview_of is None or preview is not None:
        raise MethodSpecError(f"{spec.name}: execute method must name its preview method only")
    if not spec.mutates:
        raise MethodSpecError(f"{spec.name}: execute method must declare mutates=True")
    properties, required = _required_properties(
        spec.params_schema, f"{spec.name}.params_schema"
    )
    if set(properties) != {"operationId"} or required != {"operationId"}:
        raise MethodSpecError(
            f"{spec.name}: execute params accept only operationId, never replacement fields"
        )
    if spec.params_schema.get("additionalProperties") is not False:
        raise MethodSpecError(f"{spec.name}: execute params must reject extra fields")


def _accepts_string(schema: Any) -> bool:
    if not isinstance(schema, Mapping):
        return False
    declared = schema.get("type")
    if isinstance(declared, str):
        return declared == "string"
    return isinstance(declared, (list, tuple)) and "string" in declared


def _validate_http_binding(spec: MethodSpec, *, metaschema: bool = True) -> None:
    binding = spec.http_binding
    # ``http_exposed`` is declaration eligibility, not a live transport claim.
    # A declaration may be exported before a shared identity binding exists;
    # the runtime catalog then reports HTTP_BINDING_MISSING instead of failing
    # the whole registry assembly.
    if not spec.http_exposed and binding is not None:
        raise MethodSpecError(f"{spec.name}: http_binding requires http_exposed")
    if binding is None:
        return
    if not isinstance(binding, HttpBinding) or not isinstance(
        binding.identity, IdentityBinding
    ):
        raise MethodSpecError(f"{spec.name}: http_binding is not a declared binding")
    identity = binding.identity
    if identity.mode not in (
        "none",
        "local-owner-selector",
        "local-owner-user-selector",
    ):
        raise MethodSpecError(
            f"{spec.name}: unsupported HTTP identity mode {identity.mode!r}"
        )
    if identity.mode == "none":
        if identity.param is not None:
            raise MethodSpecError(
                f"{spec.name}: identity mode none cannot name a selector parameter"
            )
    else:
        if not isinstance(identity.param, str) or not identity.param:
            raise MethodSpecError(
                f"{spec.name}: {identity.mode} needs a selector parameter"
            )
        properties = spec.params_schema.get("properties")
        if not isinstance(properties, Mapping) or identity.param not in properties:
            raise MethodSpecError(
                f"{spec.name}: selector {identity.param!r} is not a declared parameter"
            )
        # The HTTP entry injects an omitted selector before schema validation
        # and compares every supplied value with the configured operator.
        # A shared IPC schema may keep legacy aliases optional without making
        # the browser's caller identity optional at dispatch.
        if not _accepts_string(properties[identity.param]):
            raise MethodSpecError(
                f"{spec.name}: selector {identity.param!r} must accept a string"
            )
    if binding.readiness is not None and (
        not isinstance(binding.readiness, str) or not binding.readiness.strip()
    ):
        raise MethodSpecError(f"{spec.name}: readiness probe key must be a non-empty string")
    if binding.on_behalf_of is not None:
        properties = spec.params_schema.get("properties")
        if (
            not isinstance(binding.on_behalf_of, str)
            or not binding.on_behalf_of
            or not isinstance(properties, Mapping)
            or binding.on_behalf_of not in properties
        ):
            raise MethodSpecError(
                f"{spec.name}: on_behalf_of must name a declared parameter"
            )
    if binding.params_constraints is not None:
        _validate_schema(
            binding.params_constraints,
            f"{spec.name}.http_constraints",
            object_root=False,
            metaschema=metaschema,
        )
        try:
            project_http_params_schema(spec.params_schema, binding.params_constraints)
        except ValueError as error:
            raise MethodSpecError(f"{spec.name}: {error}") from error


def _http_binding_json(binding: HttpBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    document = {
        "identity": {"mode": binding.identity.mode, "param": binding.identity.param},
        "readiness": binding.readiness,
    }
    if binding.on_behalf_of is not None:
        document["onBehalfOf"] = binding.on_behalf_of
    if binding.params_constraints is not None:
        document["paramsConstraints"] = _thaw(binding.params_constraints)
    return document


def _http_example_params(value: Any, schema: Mapping[str, Any]) -> Any:
    """Project example params through the same HTTP identity boundary."""

    if not isinstance(value, Mapping):
        return value
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return dict(value)
    forbidden = {
        str(name)
        for name, declaration in properties.items()
        if isinstance(declaration, Mapping)
        and declaration.get("x-hyprial-http-forbidden") is True
    }
    return {name: item for name, item in value.items() if name not in forbidden}


def validate_method_spec(spec: MethodSpec, *, metaschema: bool = True) -> None:
    """Raise :class:`MethodSpecError` when one declaration is incomplete.

    ``metaschema=False`` skips only the Draft 2020-12 meta-schema verdict on
    the declared schemas (the dominant cost); every structural check stays.
    """

    if not isinstance(spec.name, str) or not _NAME.fullmatch(spec.name):
        raise MethodSpecError(f"{spec.name!r}: invalid method name")
    if type(spec.mutates) is not bool:
        raise MethodSpecError(f"{spec.name}: mutates must be a bool")
    if type(spec.http_exposed) is not bool:
        raise MethodSpecError(f"{spec.name}: http_exposed must be a bool")
    if spec.ipc_param_validation not in ("schema", "handler"):
        raise MethodSpecError(
            f"{spec.name}: invalid ipc_param_validation {spec.ipc_param_validation!r}"
        )
    if spec.confirmation not in ("none", "preview", "execute"):
        raise MethodSpecError(f"{spec.name}: invalid confirmation {spec.confirmation!r}")
    if not isinstance(spec.owner, str) or not _OWNER.fullmatch(spec.owner):
        raise MethodSpecError(f"{spec.name}: invalid owner {spec.owner!r}")
    _validate_schema(
        spec.params_schema, f"{spec.name}.params_schema", object_root=True, metaschema=metaschema
    )
    _validate_schema(
        spec.result_schema, f"{spec.name}.result_schema", object_root=True, metaschema=metaschema
    )
    if not spec.errors:
        raise MethodSpecError(f"{spec.name}: at least one domain error must be declared")
    codes: set[str] = set()
    for error in spec.errors:
        if not isinstance(error, MethodError) or not is_declared_error_code(error.code):
            raise MethodSpecError(f"{spec.name}: invalid error declaration {error!r}")
        if error.code in codes:
            raise MethodSpecError(f"{spec.name}: duplicate error code {error.code}")
        codes.add(error.code)
        if error.data_schema is not None:
            _validate_schema(
                error.data_schema,
                f"{spec.name}.errors[{error.code}].data",
                object_root=True,
                metaschema=metaschema,
            )
    if not spec.audience or len(set(spec.audience)) != len(spec.audience):
        raise MethodSpecError(f"{spec.name}: audience must be a non-empty unique tuple")
    unknown = set(spec.audience) - AUDIENCES
    if unknown:
        raise MethodSpecError(f"{spec.name}: unknown audience {sorted(unknown)}")
    if spec.retry not in ("safe", "idempotency-key", "never"):
        raise MethodSpecError(f"{spec.name}: invalid retry mode {spec.retry!r}")
    if spec.idempotency is not None:
        if spec.idempotency.mode != spec.retry:
            raise MethodSpecError(
                f"{spec.name}: retry and idempotency.mode disagree"
            )
        if spec.retry == "idempotency-key" and not spec.idempotency.key_field:
            raise MethodSpecError(
                f"{spec.name}: idempotency-key retry needs a key_field"
            )
        if spec.retry != "idempotency-key" and spec.idempotency.key_field is not None:
            raise MethodSpecError(
                f"{spec.name}: only idempotency-key retry may declare key_field"
            )
    if spec.mode == "unary":
        if spec.stream is not None:
            raise MethodSpecError(f"{spec.name}: unary method cannot declare a stream")
    elif spec.mode == "cursor-poll":
        stream = spec.stream
        if stream is None or not stream.cursor_field or stream.max_batch < 1:
            raise MethodSpecError(
                f"{spec.name}: cursor-poll needs cursor_field and max_batch >= 1"
            )
    else:
        raise MethodSpecError(f"{spec.name}: invalid mode {spec.mode!r}")
    if spec.phase_caller is not None and not isinstance(
        spec.phase_caller, PhaseCallerSpec
    ):
        raise MethodSpecError(f"{spec.name}: phase_caller must be a PhaseCallerSpec")
    _validate_preview(spec)
    _validate_http_binding(spec, metaschema=metaschema)
    validate_mock_echo(spec, MethodSpecError)


def validate_method_specs(
    specs: Iterable[MethodSpec], *, metaschema: bool = True
) -> tuple[MethodSpec, ...]:
    """Validate a catalog, including duplicate names and preview pairing."""

    catalog = tuple(specs)
    by_name: dict[str, MethodSpec] = {}
    for spec in catalog:
        if not isinstance(spec, MethodSpec):
            raise MethodSpecError(f"catalog entry is not a MethodSpec: {spec!r}")
        validate_method_spec(spec, metaschema=metaschema)
        if spec.name in by_name:
            raise MethodSpecError(f"duplicate method name {spec.name}")
        by_name[spec.name] = spec
    for spec in catalog:
        if spec.confirmation != "execute":
            continue
        preview = by_name.get(spec.preview_of or "")
        if preview is None or preview.confirmation != "preview":
            raise MethodSpecError(
                f"{spec.name}: preview_of must name a declared preview method"
            )
        assert preview.preview is not None
        properties, _required = _required_properties(
            spec.params_schema, f"{spec.name}.params_schema"
        )
        if preview.preview.operation_field not in properties:
            raise MethodSpecError(
                f"{spec.name}: params must use the paired operation field "
                f"{preview.preview.operation_field!r}"
            )
    return catalog


def _catalog_entry(spec: MethodSpec) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": spec.name,
        "owner": spec.owner,
        "mode": spec.mode,
        "mutates": spec.mutates,
        "retry": spec.retry,
        "audience": list(spec.audience),
        "httpExposed": spec.http_exposed,
        "httpBinding": _http_binding_json(spec.effective_http_binding),
        "confirmation": spec.confirmation,
        "paramsSchema": _thaw(spec.params_schema),
        "resultSchema": _thaw(spec.result_schema),
        "errors": [
            {
                "code": error.code,
                "description": error.description,
                "dataSchema": _thaw(error.data_schema),
            }
            for error in spec.errors
        ],
    }
    if spec.stream is not None:
        entry["stream"] = {
            "cursorField": spec.stream.cursor_field,
            "maxBatch": spec.stream.max_batch,
            "waitField": spec.stream.wait_field,
            "terminalStates": list(spec.stream.terminal_states),
        }
    if spec.idempotency is not None:
        entry["idempotency"] = {
            "mode": spec.idempotency.mode,
            "keyField": spec.idempotency.key_field,
            "scope": spec.idempotency.scope,
        }
    if spec.preview is not None:
        entry["preview"] = {
            "operationField": spec.preview.operation_field,
            "expiresField": spec.preview.expires_field,
            "summaryField": spec.preview.summary_field,
            "ttlSeconds": spec.preview.ttl_seconds,
            "binds": list(spec.preview.binds),
        }
    if spec.preview_of is not None:
        entry["previewOf"] = spec.preview_of
    if spec.ipc_param_validation != "schema":
        entry["ipcParamValidation"] = spec.ipc_param_validation
    if spec.phase_caller is not None:
        entry["phaseCaller"] = {
            "pairs": [
                {"phase": phase, "caller": caller}
                for phase, caller in spec.phase_caller.pairs
            ],
        }
    return entry


def _error_response_schema(spec: MethodSpec) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for error in spec.errors:
        properties: dict[str, Any] = {
            "code": {"const": error.code},
            "message": {"type": "string"},
        }
        if error.data_schema is not None:
            properties["data"] = _thaw(error.data_schema)
        variants.append(
            {
                "type": "object",
                "properties": properties,
                "required": ["code", "message"],
                "additionalProperties": False,
            }
        )
    if len(variants) == 1:
        return variants[0]
    return {"oneOf": variants}


def export_catalog(specs: Iterable[MethodSpec], *, metaschema: bool = True) -> dict[str, Any]:
    """Return the complete, JSON-safe method catalog in stable name order."""

    catalog = validate_method_specs(specs, metaschema=metaschema)
    return {
        "schemaVersion": 1,
        "methods": [_catalog_entry(spec) for spec in sorted(catalog, key=lambda item: item.name)],
    }


def export_openapi(
    specs: Iterable[MethodSpec],
    *,
    example_sets: Iterable[MethodExampleSet] = (),
    title: str = "Hyprial daemon request contract",
    version: str = "1",
) -> dict[str, Any]:
    """Project the catalog as deterministic OpenAPI 3.1.0 JSON.

    Every method appears in ``x-hyprial-methods``; only ``http_exposed``
    methods get a ``POST /daemon/request/<method>`` path.  When example sets
    are supplied they are validated against these same declarations and
    projected onto the operation; there is no second business schema.  There
    is no server, clock, path, home or live identity in the document.
    """

    catalog = validate_method_specs(specs)
    validated_examples = validate_method_example_sets(catalog, example_sets)
    examples_by_name = {item.method: item for item in validated_examples}
    ordered = sorted(catalog, key=lambda item: item.name)
    paths: dict[str, Any] = {}
    declarations: list[dict[str, Any]] = []
    for spec in ordered:
        params_schema = _thaw(spec.params_schema)
        binding = spec.effective_http_binding
        constraints = binding.params_constraints if binding is not None else None
        http_params_schema = project_http_params_schema(params_schema, constraints)
        result_schema = _thaw(spec.result_schema)
        operation: dict[str, Any] = {
            "operationId": spec.name,
            "summary": f"{spec.owner}: {spec.name}",
            "tags": [spec.owner],
            "x-hyprial-mode": spec.mode,
            "x-hyprial-mutates": spec.mutates,
            "x-hyprial-retry": spec.retry,
            "x-hyprial-audience": list(spec.audience),
            "x-hyprial-confirmation": spec.confirmation,
            "x-hyprial-http-binding": _http_binding_json(spec.effective_http_binding),
        }
        if spec.ipc_param_validation != "schema":
            operation["x-hyprial-ipc-param-validation"] = spec.ipc_param_validation
        if spec.phase_caller is not None:
            operation["x-hyprial-phase-caller"] = {
                "pairs": [
                    {"phase": phase, "caller": caller}
                    for phase, caller in spec.phase_caller.pairs
                ],
            }
        if spec.mock_echo is not None:
            operation["x-hyprial-mock-echo"] = dict(spec.mock_echo)
        example_set = examples_by_name.get(spec.name)
        params_examples: dict[str, Any] = {}
        if example_set is not None:
            projected = [example_to_json(case) for case in example_set.cases]
            for item in projected:
                item["params"] = _http_example_params(item["params"], params_schema)
                if constraints is not None and item["kind"] != "invalid-params":
                    if not Draft202012Validator(
                        http_params_schema, format_checker=FormatChecker(),
                    ).is_valid(item["params"]):
                        raise MethodSpecError(
                            f"{spec.name}: {item['name']} example violates HTTP constraints"
                        )
            valid_params = [
                item["params"]
                for item in projected
                if item["kind"] != "invalid-params"
            ]
            params_examples = {
                case.name: {
                    "summary": case.name,
                    "description": case.description,
                    "value": item["params"],
                }
                for case, item in zip(example_set.cases, projected)
                if item["kind"] != "invalid-params"
            }
            results = [
                item["result"]
                for item in projected
                if item["kind"] in ("success", "empty", "unconfigured")
            ]
            if valid_params:
                params_schema["examples"] = valid_params
                http_params_schema["examples"] = valid_params
            if results:
                result_schema["examples"] = results
            operation["x-hyprial-examples"] = projected
            gaps: dict[str, str] = {}
            if example_set.empty_not_applicable:
                gaps["empty"] = example_set.empty_not_applicable
            if example_set.domain_errors_not_applicable:
                gaps["domainErrors"] = example_set.domain_errors_not_applicable
            if example_set.success_not_applicable:
                gaps["success"] = example_set.success_not_applicable
            if gaps:
                operation["x-hyprial-example-gaps"] = gaps
        declaration = _catalog_entry(spec)
        for key in ("x-hyprial-examples", "x-hyprial-example-gaps", "x-hyprial-mock-echo"):
            if key in operation:
                declaration[key] = operation[key]
        declarations.append(declaration)
        if not spec.http_exposed:
            continue
        body_content: dict[str, Any] = {"schema": http_params_schema}
        if params_examples:
            body_content["examples"] = params_examples
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": body_content,
            },
        }
        operation["responses"] = {
            "200": {
                "description": f"{spec.name} result",
                "content": {
                    "application/json": {
                        "schema": result_schema,
                    }
                },
            },
            "default": {
                "description": "Domain error envelope",
                "content": {
                    "application/json": {
                        "schema": _error_response_schema(spec),
                    }
                },
            },
        }
        paths[f"/daemon/request/{spec.name}"] = {"post": operation}
    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
        "info": {"title": title, "version": version},
        "paths": paths,
        "components": {"schemas": {}},
        "x-hyprial-methods": declarations,
    }
