"""``org.use`` / ``org.resolve``: the home's current-org selection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from functools import partial

from hyprial.daemon.impl.org.membership.context import for_app
from . import schemas

from hyprial.daemon.impl.ipc.params import JsonObject
from hyprial.kernel import (
    IdempotencySpec,
    MethodError,
    MethodSpec,
    ipc_errors,
)


def _context_handler(app: Any, params: Mapping[str, Any], *, method: str, trusted_message_origin: str | None = None) -> JsonObject:
    context = for_app(app)
    if method == "org.list":
        return context.listing()
    if method == "org.use":
        return context.use(org=params.get("org"), clear=params.get("clear", False))
    return context.resolve(org=params.get("org"), space_id=params.get("spaceId"))


CONTEXT_BINDINGS = tuple(
    (MethodSpec(
        name=name,
        owner="daemon.org.membership",
        params_schema=params,
        result_schema=result,
        errors=(
            MethodError(ipc_errors.INVALID_ARGUMENT, schemas.SELECTION_ERROR),
            MethodError(ipc_errors.ORG_REQUIRED, schemas.SELECTION_ERROR),
            MethodError(ipc_errors.DAEMON_NOT_READY),
        ),
        mode="unary",
        mutates=True,
        retry="never",
        audience=("local-operator", "bound-agent"),
        http_exposed=False,
        confirmation="none",
        idempotency=IdempotencySpec(mode="never", scope="home-current-org"),
    ), partial(_context_handler, method=name))
    for name, params, result in (
        ("org.use", schemas.USE_PARAMS, schemas.USE_RESULT),
        ("org.resolve", schemas.SELECTION_PARAMS, schemas.RESOLVE_RESULT),
    )
)


__all__ = ["CONTEXT_BINDINGS"]
