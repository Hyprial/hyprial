"""Frozen §5 IPC adapter for the daemon identity resolver."""

from __future__ import annotations

import sqlite3
from typing import Any

from hyprial.identity import UserStoreError
from hyprial.kernel import DaemonRequestError, ipc_errors

from .resolver import IdentityResolver, IdentityResolverError


_METHODS = frozenset(
    {
        "identity.resolve",
        "identity.bindings.list",
        "identity.override.set",
        "identity.override.clear",
        "identity.users.list",
        "identity.users.add",
    }
)


def _operator(operator: bool) -> None:
    if not operator:
        raise DaemonRequestError(
            ipc_errors.IDENTITY_OVERRIDE_FORBIDDEN,
            "identity mutation is available only to the local operator",
        )


def _variant(
    params: dict[str, Any], *, extra: frozenset[str] = frozenset()
) -> tuple[str, str]:
    keys = set(params) - extra
    if keys == {"platform", "unionId"}:
        return "platform", "union"
    if keys == {"adapter", "openId"}:
        return "adapter", "open"
    raise DaemonRequestError(
        ipc_errors.INVALID_ARGUMENT,
        "use exactly {platform, unionId} or {adapter, openId}",
    )


#: identity.users.add accepts exactly UserStore.add_user's inputs (§5).
_USERS_ADD_FIELDS = frozenset(
    {"kind", "confirmedBy", "owner", "displayName", "nickname", "realName"}
)


def _users_add_fields(params: dict[str, Any]) -> None:
    # Keys are store-generated (member: slug(owner); guest: random), so
    # `user` or any other field is refused, never silently ignored.
    unknown = sorted(set(params) - _USERS_ADD_FIELDS)
    if unknown:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"users.add does not take {', '.join(unknown)}; the key is store-generated",
        )
    if params.get("kind") == "guest" and not params.get("displayName"):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "a guest requires a displayName"
        )


def handles_identity_ipc(method: str) -> bool:
    return method in _METHODS


def handle_identity_ipc(
    resolver: IdentityResolver,
    method: str,
    params: dict[str, Any],
    *,
    operator: bool,
) -> Any:
    try:
        if method == "identity.resolve":
            variant = _variant(params)
            if variant[0] == "platform":
                return resolver.resolve(
                    platform=params.get("platform"), union_id=params.get("unionId")
                )
            return resolver.resolve(
                adapter=params.get("adapter"), open_id=params.get("openId")
            )
        if method == "identity.bindings.list":
            return {
                "bindings": resolver.bindings(
                    platform=params.get("platform"), source=params.get("source")
                )
            }
        if method == "identity.override.set":
            # LAX(identity-step-up): R1 matches today's local-operator user bind.
            _operator(operator)
            variant = _variant(
                params, extra=frozenset({"user", "confirmedBy"})
            )
            platform = "lark"
            if variant[0] == "platform" and params.get("platform") != platform:
                raise IdentityResolverError(
                    ipc_errors.INVALID_ARGUMENT, "platform must be lark"
                )
            union_id = params.get("unionId")
            if variant[0] == "adapter":
                return resolver.set_account_override(
                    adapter=resolver._required(params.get("adapter"), "adapter"),
                    open_id=resolver._required(params.get("openId"), "openId"),
                    user_key=resolver._required(params.get("user"), "user"),
                    confirmed_by=resolver._required(
                        params.get("confirmedBy"), "confirmedBy"
                    ),
                )
            return resolver.set_override(
                platform=platform,
                union_id=resolver._required(union_id, "unionId"),
                user_key=resolver._required(params.get("user"), "user"),
                confirmed_by=resolver._required(
                    params.get("confirmedBy"), "confirmedBy"
                ),
            )
        if method == "identity.override.clear":
            # LAX(identity-step-up): R1 matches today's local-operator user bind.
            _operator(operator)
            variant = _variant(params)
            union_id = params.get("unionId")
            if variant[0] == "platform" and params.get("platform") != "lark":
                raise IdentityResolverError(
                    ipc_errors.INVALID_ARGUMENT, "platform must be lark"
                )
            if variant[0] == "adapter":
                # {cleared, unionBindingRemains?} (§5 truthful revocation).
                return resolver.clear_account_override(
                    adapter=resolver._required(params.get("adapter"), "adapter"),
                    open_id=resolver._required(params.get("openId"), "openId"),
                )
            return {
                "cleared": resolver.clear_override(
                    platform="lark",
                    union_id=resolver._required(union_id, "unionId"),
                )
            }
        if method == "identity.users.list":
            return {
                "users": resolver.list_users(
                    kind=params.get("kind"), user_key=params.get("user")
                )
            }
        if method == "identity.users.add":
            # LAX(identity-step-up): R1 matches today's local-operator user bind.
            _operator(operator)
            _users_add_fields(params)
            return {"user": resolver.add_user(params)}
    except IdentityResolverError as error:
        raise DaemonRequestError(error.code, str(error)) from error
    except UserStoreError as error:
        raise DaemonRequestError(error.code, str(error)) from error
    except (OSError, sqlite3.Error) as error:
        raise DaemonRequestError(
            ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
            f"identity source is unavailable: {type(error).__name__}",
        ) from error
    raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}")
