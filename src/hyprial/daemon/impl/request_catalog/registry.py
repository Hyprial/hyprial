"""Explicit MethodSpec→handler bindings and the single registered dispatcher.

A name is served by the registry only because it is bound here.  Families in
``MIGRATED_FAMILIES`` are closed: an undeclared name in them is refused instead
of falling back to the legacy router.  ``agent.PARTIAL_MIGRATED_METHODS``
records the opposite case for a family that is still open -- the named methods
are migrated and every other member keeps the legacy branch -- so a partial
migration can never be mistaken for a family-wide one.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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

MIGRATED_FAMILIES = frozenset({"daemon", "link"})


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


class MethodRegistry:
    """Declared methods only; missing/duplicate names and handlers are rejected."""

    def __init__(self) -> None:
        self._bindings: dict[str, BoundMethod] = {}
        self._params: dict[str, Draft202012Validator] = {}
        self._results: dict[str, Draft202012Validator] = {}
        self._frozen = False

    @classmethod
    def from_bindings(
        cls,
        bindings: Iterable[
            tuple[MethodSpec, MethodHandler]
            | tuple[MethodSpec, MethodHandler, ParamErrorPresenter]
        ],
    ) -> "MethodRegistry":
        registry = cls()
        for binding in bindings:
            spec, handler, *presenter = binding
            registry.bind(
                spec,
                handler,
                param_error=presenter[0] if presenter else None,
            )
        registry.freeze()
        return registry

    def bind(
        self,
        spec: MethodSpec,
        handler: MethodHandler,
        *,
        param_error: ParamErrorPresenter | None = None,
    ) -> None:
        if self._frozen:
            raise RuntimeError("method registry is frozen; bindings are assembly-time")
        if not isinstance(spec, MethodSpec):
            raise TypeError("bind() needs a MethodSpec declaration")
        if not callable(handler):
            raise TypeError(f"{spec.name}: handler is missing or not callable")
        if param_error is not None and not callable(param_error):
            raise TypeError(f"{spec.name}: param_error presenter must be callable")
        if spec.name in self._bindings:
            raise ValueError(f"duplicate method binding {spec.name}")
        self._bindings[spec.name] = BoundMethod(spec, handler, param_error)

    def freeze(self) -> None:
        """Validate the complete catalog and build its validators once."""

        if self._frozen:
            return
        specs = validate_method_specs(binding.spec for binding in self._bindings.values())
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
        checker = FormatChecker()
        self._params = {
            spec.name: Draft202012Validator(_plain(spec.params_schema), format_checker=checker)
            for spec in specs
        }
        self._results = {
            spec.name: Draft202012Validator(_plain(spec.result_schema), format_checker=checker)
            for spec in specs
        }
        self._frozen = True

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
        self._refuse_params(binding, params)
        try:
            result = binding.handler(
                app, params, trusted_message_origin=trusted_message_origin
            )
        except DaemonRequestError as error:
            self._check_declared_error(binding, error)
            raise
        self._refuse_invalid(
            self._results[method],
            result,
            method,
            kind="result",
            code=ipc_errors.DAEMON_ERROR,
        )
        return result

    def _refuse_params(self, binding: BoundMethod, params: Mapping[str, Any]) -> None:
        method = binding.spec.name
        error = next(
            iter(sorted(self._params[method].iter_errors(params), key=_error_order)),
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
    from .agent import AGENT_BINDINGS
    from .agent.provider import AGENT_PROVIDER_BINDINGS
    from .daemon.catalog import CATALOG_BINDINGS
    from .link import LINK_BINDINGS
    from .daemon.message_lookup import MESSAGE_LOOKUP_BINDINGS
    from .session import SESSION_BINDINGS

    return MethodRegistry.from_bindings(
        (*CATALOG_BINDINGS, *LINK_BINDINGS, *MESSAGE_LOOKUP_BINDINGS,
         *SESSION_BINDINGS, *AGENT_BINDINGS, *AGENT_PROVIDER_BINDINGS)
    )


METHOD_REGISTRY: MethodRegistry = _build_registry()

#: Kept as an explicit name for callers that prefer a registry constructor
#: reference over the process-wide, frozen assembly.
BUILD_REGISTRY: Callable[[], MethodRegistry] = _build_registry


def daemon_catalog() -> JsonObject:
    """Return the complete declared catalog as pure JSON-safe data."""

    from hyprial.kernel import export_catalog

    return export_catalog(METHOD_REGISTRY.specs)


def daemon_openapi() -> JsonObject:
    """Return the deterministic contract projection; no HTTP server starts."""

    from hyprial.kernel import export_openapi

    from .agent.examples import REQUEST_EXAMPLES

    return export_openapi(METHOD_REGISTRY.specs, example_sets=REQUEST_EXAMPLES)
