"""Explicit MethodSpec→handler bindings and the single registered dispatcher.

UA1b (2026-11-18): after parameter validation, every method passes
``user_agent.admin.refuse_user_agent_method``, which refuses agent
administration aimed at the owner's user agent with USER_AGENT_IMMUTABLE
(methods that authorize in their handler refuse it there instead).

A name is served by the registry only because it is bound here.  Families in
``MIGRATED_FAMILIES`` are closed: an undeclared name in them is refused instead
of falling back to the legacy router. ``agent.PARTIAL_MIGRATED_METHODS`` records
the opposite case for a family that is still open: its named methods are
migrated and every other member keeps the legacy branch.

``legacy_params`` bindings keep their historical IPC parameter domain and
error order.  The HTTP listener still validates its own declared projection
before dispatch, so this is not a browser-side relaxation.

Start-path cost decision: assembling the process-wide registry runs only the
cheap structural checks (names, handlers, preview pairing, readiness probes).
The Draft 2020-12 meta-schema verdict on every declared schema measured at
about 90% of the import (``check_schema`` over ~370 schemas, seconds on a
loaded runner), and per-method validators were built eagerly although a
process serves a handful of methods.  Both moved off the start path:
``MethodRegistry.verify()`` / ``verify_registry()`` run the full declaration
self-check (meta-schema included, plus every validator construction) and the
test suite and CI gates call it, so an invalid declaration still turns CI red;
request/response validators are built on first use, under a lock, and cached.
Validation outcomes are identical; only *when* a bad declaration is reported
moves, from import to ``verify()``.
"""

from __future__ import annotations

from .migration import MIGRATED_FAMILIES as MIGRATED_FAMILIES

import copy
import functools
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from hyprial.kernel import (
    DaemonRequestError,
    MethodSpec,
    MethodSpecError,
    ipc_errors,
    validate_method_specs,
)

JsonObject = dict[str, Any]


ParamErrorPresenter = Callable[[Any, Mapping[str, Any]], str]


class MethodHandler(Protocol):
    """A handler receives the application and the validated params."""

    def __call__(
        self,
        app: Any,
        params: Mapping[str, Any],
        *,
        trusted_message_origin: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class BoundMethod:
    spec: MethodSpec
    handler: MethodHandler
    param_error: ParamErrorPresenter | None = None
    legacy_params: bool = False
    preauthorize: MethodHandler | None = None


class MethodRegistry:
    """Declared methods only; missing/duplicate names and handlers are rejected."""

    def __init__(self) -> None:
        self._bindings: dict[str, BoundMethod] = {}
        self._params: dict[str, Draft202012Validator] = {}
        self._results: dict[str, Draft202012Validator] = {}
        self._validator_lock = threading.Lock()
        self._format_checker = FormatChecker()
        self._frozen = False

    @classmethod
    def from_bindings(
        cls,
        bindings: Iterable[
            tuple[MethodSpec, MethodHandler]
            | tuple[MethodSpec, MethodHandler, ParamErrorPresenter]
            | tuple[MethodSpec, MethodHandler, ParamErrorPresenter | None, bool]
            | BoundMethod
        ],
    ) -> "MethodRegistry":
        registry = cls()
        for binding in bindings:
            if not isinstance(binding, BoundMethod):
                spec, handler, *options = binding
                binding = BoundMethod(
                    spec, handler,
                    param_error=options[0] if len(options) >= 1 else None,
                    legacy_params=options[1] if len(options) >= 2 else False,
                )
            registry.bind(
                binding.spec,
                binding.handler,
                param_error=binding.param_error,
                legacy_params=binding.legacy_params,
                preauthorize=binding.preauthorize,
            )
        registry.freeze()
        return registry

    def bind(
        self,
        spec: MethodSpec,
        handler: MethodHandler,
        *,
        param_error: ParamErrorPresenter | None = None,
        legacy_params: bool = False,
        preauthorize: MethodHandler | None = None,
    ) -> None:
        if self._frozen:
            raise RuntimeError("method registry is frozen; bindings are assembly-time")
        if not isinstance(spec, MethodSpec):
            raise TypeError("bind() needs a MethodSpec declaration")
        if not callable(handler):
            raise TypeError(f"{spec.name}: handler is missing or not callable")
        if param_error is not None and not callable(param_error):
            raise TypeError(f"{spec.name}: param_error presenter must be callable")
        if preauthorize is not None and not callable(preauthorize):
            raise TypeError(f"{spec.name}: preauthorize must be callable")
        if spec.name in self._bindings:
            raise ValueError(f"duplicate method binding {spec.name}")
        self._bindings[spec.name] = BoundMethod(
            spec, handler, param_error=param_error, legacy_params=legacy_params,
            preauthorize=preauthorize,
        )

    def freeze(self) -> None:
        """Run the cheap structural checks once; validators are built lazily.

        The meta-schema verdict is deferred to :meth:`verify`.
        """

        if self._frozen:
            return
        specs = validate_method_specs(
            (binding.spec for binding in self._bindings.values()), metaschema=False
        )
        declared = {spec.name for spec in specs}
        if declared != set(self._bindings):
            raise ValueError("method catalog and handler bindings disagree")
        from .daemon.readiness import READINESS_PROBES

        for spec in specs:
            binding = spec.http_binding
            if binding is not None and binding.readiness not in (None, *READINESS_PROBES):
                raise MethodSpecError(
                    f"{spec.name}: readiness probe {binding.readiness!r} is not assembled"
                )
        self._frozen = True

    def verify(self) -> None:
        """Full declaration self-check: meta-schema, structure and validators.

        Raises :class:`MethodSpecError` (or the schema error a validator
        construction reports) for an invalid declaration.  Not run at import.
        """

        specs = validate_method_specs(
            (binding.spec for binding in self._bindings.values()), metaschema=True
        )
        for spec in specs:
            self._params_validator(spec.name)
            self._results_validator(spec.name)

    def _params_validator(self, method: str) -> Draft202012Validator:
        return self._lazy_validator(self._params, method, "params_schema")

    def _results_validator(self, method: str) -> Draft202012Validator:
        return self._lazy_validator(self._results, method, "result_schema")

    def _lazy_validator(
        self, cache: dict[str, Draft202012Validator], method: str, attribute: str
    ) -> Draft202012Validator:
        validator = cache.get(method)
        if validator is not None:
            return validator
        with self._validator_lock:
            validator = cache.get(method)
            if validator is None:
                schema = getattr(self._bindings[method].spec, attribute)
                validator = Draft202012Validator(
                    _plain(schema), format_checker=self._format_checker
                )
                cache[method] = validator
            return validator

    def __contains__(self, method: object) -> bool:
        return method in self._bindings

    def __len__(self) -> int:
        return len(self._bindings)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    @property
    def specs(self) -> tuple[MethodSpec, ...]:
        return tuple(self._bindings[name].spec for name in self.names)

    def get(self, method: str) -> BoundMethod | None:
        return self._bindings.get(method)

    def invoke(
        self,
        app: Any,
        method: str,
        params: Mapping[str, Any],
        *,
        trusted_message_origin: str | None = None,
    ) -> Any:
        """Validate params, run the bound handler, then validate its result."""

        binding = self.get(method)
        if binding is None:
            raise DaemonRequestError(
                ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
            )
        if not isinstance(params, Mapping):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, f"{method} params must be an object"
            )
        if binding.preauthorize is not None:
            try:
                binding.preauthorize(
                    app, params, trusted_message_origin=trusted_message_origin
                )
            except DaemonRequestError as error:
                self._check_declared_error(binding, error)
                raise
        if binding.spec.ipc_param_validation == "schema":
            self._refuse_params(binding, params)
        from hyprial.daemon.impl.user_agent.admin import refuse_user_agent_method

        try:
            refuse_user_agent_method(app, binding.spec, params)
        except DaemonRequestError as error:
            self._check_declared_error(binding, error)
            raise
        try:
            result = binding.handler(
                app, params, trusted_message_origin=trusted_message_origin
            )
        except DaemonRequestError as error:
            self._check_declared_error(binding, error)
            raise
        self._refuse_invalid(
            self._results_validator(method),
            result,
            method,
            kind="result",
            code=ipc_errors.DAEMON_ERROR,
        )
        return result

    def _refuse_params(self, binding: BoundMethod, params: Mapping[str, Any]) -> None:
        if binding.legacy_params:
            return
        method = binding.spec.name
        error = next(
            iter(sorted(self._params_validator(method).iter_errors(params), key=_error_order)),
            None,
        )
        if error is None:
            return
        if binding.param_error is not None:
            message = binding.param_error(error, params)
        else:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            message = f"{method} params do not match the declared schema at {path}"
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, message)

    @staticmethod
    def _check_declared_error(binding: BoundMethod, error: DaemonRequestError) -> None:
        declared = {item.code: item for item in binding.spec.errors}
        spec_error = declared.get(error.code)
        if spec_error is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_ERROR,
                f"{binding.spec.name} handler raised an undeclared error code",
            )
        if error.data is None:
            return
        if spec_error.data_schema is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_ERROR,
                f"{binding.spec.name} handler attached undeclared error data",
            )
        validator = Draft202012Validator(
            _plain(spec_error.data_schema), format_checker=FormatChecker()
        )
        if next(iter(sorted(validator.iter_errors(error.data), key=_error_order)), None) is not None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_ERROR,
                f"{binding.spec.name} error data violates its declared schema",
            )

    @staticmethod
    def _refuse_invalid(
        validator: Draft202012Validator,
        value: Any,
        method: str,
        *,
        kind: str,
        code: str,
    ) -> None:
        error = next(iter(sorted(validator.iter_errors(value), key=_error_order)), None)
        if error is None:
            return
        path = "/".join(str(part) for part in error.absolute_path) or "<root>"
        # The jsonschema message may quote the original value (potentially a
        # secret).  The contract failure keeps only the declared path.
        raise DaemonRequestError(
            code, f"{method} {kind} do not match the declared schema at {path}"
        )


def _plain(value: Any) -> Any:
    """Detached plain dict/list copy of a frozen schema declaration."""

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _error_order(error: Any) -> tuple[str, ...]:
    return tuple(str(part) for part in error.absolute_path)


def _build_registry() -> MethodRegistry:
    from .adapter import ADAPTER_BINDINGS
    from .adapter.lifecycle import ADAPTER_LIFECYCLE_BINDINGS
    from .adapter.identities import ADAPTER_IDENTITY_BINDINGS
    from .adapter.registration import ADAPTER_REGISTRATION_BINDINGS
    from .adapter.authorization import ADAPTER_AUTHORIZATION_BINDINGS
    from .adapter.removal import BINDINGS as ADAPTER_REMOVAL_BINDINGS
    from .adapter.routes import BINDINGS as ADAPTER_ROUTE_BINDINGS
    from .agent import AGENT_BINDINGS
    from .agent.attach import AGENT_ATTACH_BINDINGS
    from .agent.grant_simulation import BINDINGS as GRANT_SIMULATION_BINDINGS
    from .agent.lifecycle import AGENT_LIFECYCLE_BINDINGS
    from .agent.provider import AGENT_PROVIDER_BINDINGS
    from .agent.provider_auth import BINDINGS as NATIVE_AUTH_BINDINGS
    from .agent.relations import RELATIONS_BINDINGS
    from .agent.rename import AGENT_RENAME_BINDINGS
    from .agent.destroy import BINDINGS as AGENT_DESTROY_BINDINGS, admit as destroy_admit
    from .daemon.catalog import CATALOG_BINDINGS
    from .migration import validate_migration_coverage
    from .upgrade import UPGRADE_BINDINGS
    from .identity import IDENTITY_PRODUCTION_READ_BINDINGS
    from .identity.writes import IDENTITY_WRITE_BINDINGS
    from .identity.binding import BINDINGS as IDENTITY_BINDING_METHODS
    from .message import MESSAGE_STATUS_BINDING, MESSAGE_HISTORY_BINDING, MESSAGE_QUERY_BINDING, PENDING_BATCH_BINDING
    from .message.query import OUTBOX_LIST_BINDING
    from .message.directory import CHAT_DIRECTORY_BINDINGS
    from .message.progress import CHAT_PROGRESS_BINDINGS
    from .message.send import MESSAGE_SEND_BINDINGS, MESSAGE_SEND_KEY_LOOKUP_BINDINGS
    from .network import NETWORK_BINDINGS
    from .maintenance import MAINTENANCE_BINDINGS
    from .daemon.message_lookup import MESSAGE_LOOKUP_BINDINGS
    from .org import ORG_BINDINGS
    from .onboarding.handlers import ONBOARDING_BINDINGS
    from .service import SERVICE_BINDINGS
    from .workflow_history import BINDINGS as WORKFLOW_HISTORY_BINDINGS
    from .routine import ROUTINE_BINDINGS
    from .routine.plan import BINDINGS as ROUTINE_PLAN_BINDINGS
    from .workflow import BINDINGS as WORKFLOW_CONTEXT_BINDINGS
    from .pac_gc import BINDINGS as PAC_GC_BINDINGS
    from .session import SESSION_BINDINGS
    from .setup import SETUP_BINDINGS
    from .model import BINDINGS as MODEL_BINDINGS, admit as model_admit
    from .setup.routes import SPEC as ROUTE_SPEC, handle as route_handle, admit as route_admit
    from .setup.probe import SPEC as PROBE_SPEC, handle as probe_handle, admit as probe_admit
    from .teleport import TELEPORT_BINDINGS
    from .conversations import CONVERSATION_BINDINGS
    from .conversations.groups import GROUP_BINDINGS

    from hyprial.daemon.impl.user_key.ipc import USER_KEY_BINDINGS, preauthorize
    from hyprial.daemon.impl.provider_keys.ipc import (
        PROVIDER_KEY_BINDINGS, preauthorize as pk_preauthorize,
    )

    user_keys = (
        BoundMethod(spec, handler, preauthorize=partial(preauthorize, spec.name))
        for spec, handler in USER_KEY_BINDINGS
    )
    owner_keys = (
        BoundMethod(spec, handler, preauthorize=partial(pk_preauthorize, spec.name))
        for spec, handler in PROVIDER_KEY_BINDINGS
    )
    registry = MethodRegistry.from_bindings((
        *CATALOG_BINDINGS, *ADAPTER_BINDINGS, *ADAPTER_LIFECYCLE_BINDINGS,
        *ADAPTER_IDENTITY_BINDINGS, *ADAPTER_REGISTRATION_BINDINGS,
        *ADAPTER_AUTHORIZATION_BINDINGS,
        *(BoundMethod(spec, handler) for spec, handler in ADAPTER_REMOVAL_BINDINGS),
        *ADAPTER_ROUTE_BINDINGS,
        *NETWORK_BINDINGS, *MESSAGE_LOOKUP_BINDINGS,
        *MAINTENANCE_BINDINGS, *IDENTITY_PRODUCTION_READ_BINDINGS,
        MESSAGE_STATUS_BINDING, MESSAGE_HISTORY_BINDING, MESSAGE_QUERY_BINDING,
        OUTBOX_LIST_BINDING,
        PENDING_BATCH_BINDING, *CHAT_DIRECTORY_BINDINGS, *CHAT_PROGRESS_BINDINGS,
        *MESSAGE_SEND_BINDINGS, *MESSAGE_SEND_KEY_LOOKUP_BINDINGS,
        *SESSION_BINDINGS, *AGENT_BINDINGS, *AGENT_LIFECYCLE_BINDINGS,
        *AGENT_PROVIDER_BINDINGS, *NATIVE_AUTH_BINDINGS, *AGENT_ATTACH_BINDINGS, *AGENT_RENAME_BINDINGS,
        *(BoundMethod(spec, handler, preauthorize=destroy_admit) for spec, handler in AGENT_DESTROY_BINDINGS),
        *RELATIONS_BINDINGS, *WORKFLOW_HISTORY_BINDINGS, *GRANT_SIMULATION_BINDINGS,
        *IDENTITY_WRITE_BINDINGS, *IDENTITY_BINDING_METHODS, *SETUP_BINDINGS, *ORG_BINDINGS,
        BoundMethod(ROUTE_SPEC, route_handle, preauthorize=route_admit),
        *ONBOARDING_BINDINGS, *SERVICE_BINDINGS, *ROUTINE_BINDINGS, *ROUTINE_PLAN_BINDINGS, *WORKFLOW_CONTEXT_BINDINGS, *PAC_GC_BINDINGS,
        *CONVERSATION_BINDINGS, *GROUP_BINDINGS, *UPGRADE_BINDINGS, *TELEPORT_BINDINGS,
        *user_keys, *owner_keys,
        BoundMethod(PROBE_SPEC, probe_handle, preauthorize=probe_admit),
        *(BoundMethod(spec, handler, preauthorize=model_admit) for spec, handler in MODEL_BINDINGS),
    ))

    validate_migration_coverage(registry.specs)
    return registry


METHOD_REGISTRY: MethodRegistry = _build_registry()

#: Kept as an explicit name for callers that prefer a registry constructor
#: reference over the process-wide, frozen assembly.
BUILD_REGISTRY: Callable[[], MethodRegistry] = _build_registry


def verify_registry() -> None:
    """Run the full declaration self-check on the process-wide registry."""

    METHOD_REGISTRY.verify()


def export_catalog(specs: Iterable[MethodSpec], *, metaschema: bool = True) -> JsonObject:
    """Validate and project declarations (kernel ``export_catalog``)."""

    from hyprial.kernel import export_catalog as kernel_export_catalog

    return kernel_export_catalog(specs, metaschema=metaschema)


@functools.lru_cache(maxsize=1)
def _frozen_catalog() -> JsonObject:
    """The frozen registry's catalog, validated and projected once per process.

    ``daemon.catalog`` sits on the HTTP entry's handshake budget; re-running
    every schema check per call grew with the declared vocabulary.  Callers
    must not mutate the result; ``daemon_catalog`` hands out a copy.  The
    meta-schema verdict is :func:`verify_registry`'s job, not the first
    handshake's.
    """

    return export_catalog(METHOD_REGISTRY.specs, metaschema=False)


def daemon_catalog() -> JsonObject:
    """Return the complete declared catalog as pure JSON-safe data."""

    return copy.deepcopy(_frozen_catalog())


def daemon_openapi() -> JsonObject:
    """Return the deterministic contract projection; no HTTP server starts."""

    from hyprial.daemon.impl.provider_keys.examples import EXAMPLES as PROVIDER_KEY_EXAMPLES
    from hyprial.kernel import export_openapi

    from .adapter.examples import ADAPTER_EXAMPLES
    from .identity.examples import IDENTITY_EXAMPLES
    from .agent.cell import CELL_EXAMPLES
    from .adapter.identities import ADAPTER_IDENTITY_EXAMPLES
    from .adapter.registration import ADAPTER_REGISTRATION_EXAMPLES
    from .adapter.removal.examples import EXAMPLES as ADAPTER_REMOVAL_EXAMPLES
    from .adapter.authorization import ADAPTER_AUTHORIZATION_EXAMPLES
    from .adapter.routes.examples import EXAMPLES as ADAPTER_ROUTE_EXAMPLES
    from .agent.grant_simulation.examples import EXAMPLES as GRANT_SIMULATION_EXAMPLES
    from .agent.examples import REQUEST_EXAMPLES
    from .identity.write_examples import IDENTITY_WRITE_EXAMPLES
    from .identity.binding.examples import EXAMPLES as IDENTITY_BINDING_EXAMPLES
    from .network.examples import NETWORK_EXAMPLES
    from .maintenance import MAINTENANCE_EXAMPLES
    from .message.examples import MESSAGE_EXAMPLES, OUTBOX_EXAMPLES
    from .message.directory import TARGETS_EXAMPLES
    from .message.progress import PROGRESS_LIST_EXAMPLES
    from .message.send import MESSAGE_SEND_EXAMPLES, MESSAGE_SEND_KEY_LOOKUP_EXAMPLES
    from .org.examples import ORG_REQUEST_EXAMPLES
    from .org.membership.examples import MEMBERSHIP_EXAMPLES
    from .org.management_examples import ORG_MANAGEMENT_EXAMPLES
    from .org.management_examples.examples import TOP_INSPECT_EXAMPLES
    from .org.orgfs.read_examples import ORGFS_READ_EXAMPLES
    from .onboarding import ONBOARDING_EXAMPLES
    from .pac_gc.examples import EXAMPLES as PAC_GC_EXAMPLES
    from .org.orgfs.write_examples import ORGFS_WRITE_EXAMPLES
    from .service.examples import SERVICE_EXAMPLES
    from .workflow_history import EXAMPLES as WORKFLOW_HISTORY_EXAMPLES
    from .routine.examples import ROUTINE_EXAMPLES
    from .routine.plan_examples import EXAMPLES as ROUTINE_PLAN_EXAMPLES
    from .workflow.examples import EXAMPLES as WORKFLOW_CONTEXT_EXAMPLES
    from .setup import SETUP_EXAMPLES
    from .model.examples import EXAMPLES as MODEL_EXAMPLES
    from .setup.routes import EXAMPLES as ROUTE_EXAMPLES
    from .setup.probe import EXAMPLES as PROBE_EXAMPLES
    from .conversations.examples import CONVERSATION_EXAMPLES

    return export_openapi(
        METHOD_REGISTRY.specs,
        example_sets=(
            *REQUEST_EXAMPLES, *GRANT_SIMULATION_EXAMPLES, *ADAPTER_EXAMPLES, *CELL_EXAMPLES, *ADAPTER_IDENTITY_EXAMPLES,
            *ADAPTER_REGISTRATION_EXAMPLES, *ADAPTER_REMOVAL_EXAMPLES,
            *ADAPTER_AUTHORIZATION_EXAMPLES,
            *ADAPTER_ROUTE_EXAMPLES,
            *NETWORK_EXAMPLES, *MESSAGE_EXAMPLES, *OUTBOX_EXAMPLES, MESSAGE_SEND_EXAMPLES,
            *IDENTITY_EXAMPLES, *IDENTITY_BINDING_EXAMPLES, *MAINTENANCE_EXAMPLES,
            MESSAGE_SEND_KEY_LOOKUP_EXAMPLES, TARGETS_EXAMPLES,
            PROGRESS_LIST_EXAMPLES, *IDENTITY_WRITE_EXAMPLES, *SETUP_EXAMPLES, *ROUTE_EXAMPLES, PROBE_EXAMPLES,
            *ORG_REQUEST_EXAMPLES, *MEMBERSHIP_EXAMPLES,
            *ORG_MANAGEMENT_EXAMPLES, *TOP_INSPECT_EXAMPLES, *ORGFS_READ_EXAMPLES, *ORGFS_WRITE_EXAMPLES,
            *ONBOARDING_EXAMPLES, *SERVICE_EXAMPLES, *ROUTINE_EXAMPLES, *ROUTINE_PLAN_EXAMPLES, *WORKFLOW_CONTEXT_EXAMPLES,
            *PAC_GC_EXAMPLES, *WORKFLOW_HISTORY_EXAMPLES,
            *CONVERSATION_EXAMPLES, *PROVIDER_KEY_EXAMPLES,
            *MODEL_EXAMPLES,
        ),
    )
