"""Validation and freezing helpers for ``MethodSpec.mock_echo`` declarations.

Decision record (2026-11-21):
- Existing metadata (``idempotency``, ``params_schema``, ``result_schema``)
  cannot unambiguously map caller-minted request keys to result fields because
  write methods use ``retry="never"`` and response field names differ from
  request parameter names (``idempotencyKey`` -> ``operationId``,
  ``operationKey`` -> ``commandId``).
- Methods declare ``mock_echo`` explicitly on ``MethodSpec``, projected into
  OpenAPI as ``x-hyprial-mock-echo`` so the Contract Mock echoes caller-minted
  keys generically without naming any daemon method.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..method_spec import MethodSpec


def freeze_mock_echo(
    value: Mapping[str, str] | None, method_name: str
) -> Mapping[str, str] | None:
    """Freeze a param-to-result echo mapping at MethodSpec construction."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{method_name}: mock_echo must be a non-empty mapping")
    frozen: dict[str, str] = {}
    for param_key, result_key in sorted(value.items()):
        if not isinstance(param_key, str) or not param_key:
            raise ValueError(f"{method_name}: mock_echo param key must be a non-empty string")
        if not isinstance(result_key, str) or not result_key:
            raise ValueError(f"{method_name}: mock_echo result key must be a non-empty string")
        frozen[param_key] = result_key
    return MappingProxyType(frozen)


def validate_mock_echo(spec: "MethodSpec", error_cls: type[Exception]) -> None:
    """Ensure every mock_echo entry names declared HTTP param and result fields."""

    echo = spec.mock_echo
    if echo is None:
        return
    if not isinstance(echo, Mapping) or not echo:
        raise error_cls(f"{spec.name}: mock_echo must be a non-empty mapping")
    params_props = spec.params_schema.get("properties")
    result_props = spec.result_schema.get("properties")
    if not isinstance(params_props, Mapping) or not isinstance(result_props, Mapping):
        raise error_cls(f"{spec.name}: mock_echo requires object params and result properties")
    for param_key, result_key in echo.items():
        if not isinstance(param_key, str) or not param_key or param_key not in params_props:
            raise error_cls(f"{spec.name}: mock_echo param {param_key!r} is not declared")
        declaration = params_props[param_key]
        if isinstance(declaration, Mapping) and declaration.get("x-hyprial-http-forbidden") is True:
            raise error_cls(f"{spec.name}: mock_echo param {param_key!r} is HTTP-forbidden")
        if not isinstance(result_key, str) or not result_key or result_key not in result_props:
            raise error_cls(f"{spec.name}: mock_echo result {result_key!r} is not declared")
