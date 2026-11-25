"""Non-HTTP request surface for persistent managed harness arguments."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from functools import partial
from typing import Any

from hyprial.daemon.impl.adapters.spec import is_adapter_platform
from hyprial.daemon.impl.user_agent.naming import refuse_user_agent_target
from hyprial.identity import AgentError, normalize_harness_args
from hyprial.kernel import (
    DaemonRequestError,
    HarnessArgValidationError,
    IdempotencySpec,
    MethodError,
    MethodSpec,
    classify_dangerous_launch_args,
    current_uid,
    ipc_errors,
    validate_harness_launch_args,
)

from ..entity import agent_status_row

JsonObject = dict[str, Any]

GET = "agent.harness-args.get"
VALIDATE = "agent.harness-args.validate"
SET = "agent.harness-args.set"
CLEAR = "agent.harness-args.clear"
NAMES = frozenset({GET, VALIDATE, SET, CLEAR})

_SUPPORTED = frozenset({"claude", "codex", "pi", "agy"})
_UNSUPPORTED = frozenset({"jev"})

_STRING = {"type": "string", "minLength": 1}
_ARG_LIST = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": 256,
}
_STORED_ARG_LIST = {"type": "array", "items": {"type": "string"}}
_STORED_ARG_MAP = {
    "type": "object",
    "propertyNames": {"type": "string", "minLength": 1},
    "additionalProperties": _STORED_ARG_LIST,
}
_DANGEROUS_ROW = {
    "type": "object",
    "properties": {
        "token": _STRING,
        "category": _STRING,
        "reason": _STRING,
    },
    "required": ["token", "category", "reason"],
    "additionalProperties": False,
}
_PREVIEW = {
    "type": "object",
    "properties": {
        "harness": _STRING,
        "args": _STORED_ARG_LIST,
        "valid": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "dangerous": {"type": "array", "items": _DANGEROUS_ROW},
    },
    "required": [
        "harness", "args", "valid", "issues", "warnings", "dangerous",
    ],
    "additionalProperties": False,
}

_GET_PARAMS = {
    "type": "object",
    "properties": {"name": _STRING},
    "required": ["name"],
    "additionalProperties": False,
}
_VALIDATE_PARAMS = {
    "type": "object",
    "properties": {"harness": _STRING, "args": _ARG_LIST},
    "required": ["harness", "args"],
    "additionalProperties": False,
}
_SET_PARAMS = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "harness": _STRING,
        "args": _ARG_LIST,
        "expectedHarnessArgs": _STORED_ARG_MAP,
        "confirmDangerous": {"type": "boolean"},
        "reason": {"type": ["string", "null"], "maxLength": 512},
    },
    "required": ["name", "harness", "args", "expectedHarnessArgs"],
    "additionalProperties": False,
}
_CLEAR_PARAMS = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "harness": {"type": ["string", "null"], "minLength": 1},
        "all": {"type": "boolean"},
        "confirmAll": {"type": "boolean"},
        "expectedHarnessArgs": _STORED_ARG_MAP,
        "reason": {"type": ["string", "null"], "maxLength": 512},
    },
    "required": ["name", "expectedHarnessArgs"],
    "additionalProperties": False,
}

_AGENT_ROW = agent_status_row(with_activity=False)
_GET_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "agent": _AGENT_ROW,
        "harnessArgs": _STORED_ARG_MAP,
        "harnesses": {"type": "array", "items": _PREVIEW},
    },
    "required": ["ok", "agent", "harnessArgs", "harnesses"],
    "additionalProperties": False,
}
_VALIDATE_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "harness": _STRING,
        "args": _ARG_LIST,
        "valid": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "dangerous": {"type": "array", "items": _DANGEROUS_ROW},
    },
    "required": [
        "ok", "harness", "args", "valid", "issues", "warnings", "dangerous",
    ],
    "additionalProperties": False,
}
_MUTATION_RESULT = {
    "type": "object",
    "properties": {
        "ok": {"const": True},
        "changed": {"type": "boolean"},
        "agent": _AGENT_ROW,
        "harnessArgs": _STORED_ARG_MAP,
        "auditId": {"type": ["string", "null"]},
    },
    "required": ["ok", "changed", "agent", "harnessArgs", "auditId"],
    "additionalProperties": False,
}

_ISSUE_DATA = {
    "type": "object",
    "properties": {
        "harness": _STRING,
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": _STRING,
                    "message": _STRING,
                    "token": {"type": ["string", "null"]},
                },
                "required": ["code", "message", "token"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["harness", "issues"],
    "additionalProperties": False,
}
_DANGEROUS_DATA = {
    "type": "object",
    "properties": {
        "harness": _STRING,
        "dangerous": {"type": "array", "items": _DANGEROUS_ROW},
    },
    "required": ["harness", "dangerous"],
    "additionalProperties": False,
}
_CONFLICT_DATA = {
    "type": "object",
    "properties": {
        "action": {"const": "re-read"},
        "method": {"const": GET},
    },
    "required": ["action", "method"],
    "additionalProperties": False,
}


def _error(code: str, *, data: Mapping[str, Any] | None = None) -> MethodError:
    return MethodError(
        code,
        description="The request was refused without mutating harness arguments.",
        data_schema=data,
    )


_READ_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    _error(ipc_errors.AGENT_NOT_FOUND),
    _error(AgentError.code),
    _error(ipc_errors.DAEMON_ERROR),
)
_SUPPORT_ERRORS = (
    _error(ipc_errors.USE_ADAPTER_COMMAND),
    _error(ipc_errors.UNSUPPORTED_CAPABILITY),
)
_VALIDATE_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    *_SUPPORT_ERRORS,
    _error(ipc_errors.DAEMON_ERROR),
)
_SET_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    _error(ipc_errors.CALLER_NOT_AUTHORIZED),
    _error(ipc_errors.AGENT_NOT_FOUND),
    _error(ipc_errors.HARNESS_ARGS_CONFLICT, data=_CONFLICT_DATA),
    _error(ipc_errors.HARNESS_ARGS_INVALID, data=_ISSUE_DATA),
    _error(
        ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
        data=_DANGEROUS_DATA,
    ),
    *_SUPPORT_ERRORS,
    _error(AgentError.code),
    _error(ipc_errors.DAEMON_ERROR),
)
_CLEAR_ERRORS = (
    _error(ipc_errors.INVALID_ARGUMENT),
    _error(ipc_errors.CALLER_NOT_AUTHORIZED),
    _error(ipc_errors.AGENT_NOT_FOUND),
    _error(ipc_errors.HARNESS_ARGS_CONFLICT, data=_CONFLICT_DATA),
    _error(AgentError.code),
    _error(ipc_errors.DAEMON_ERROR),
)

GET_SPEC = MethodSpec(
    name=GET,
    owner="daemon.agents",
    params_schema=_GET_PARAMS,
    result_schema=_GET_RESULT,
    errors=_READ_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="harness-args-read"),
)
VALIDATE_SPEC = MethodSpec(
    name=VALIDATE,
    owner="daemon.agents",
    params_schema=_VALIDATE_PARAMS,
    result_schema=_VALIDATE_RESULT,
    errors=_VALIDATE_ERRORS,
    mode="unary",
    mutates=False,
    retry="safe",
    audience=("local-operator", "bound-agent"),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="harness-args-validate"),
)
SET_SPEC = MethodSpec(
    name=SET,
    owner="daemon.agents",
    params_schema=_SET_PARAMS,
    result_schema=_MUTATION_RESULT,
    errors=(*_SET_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="harness-args-set"),
)
CLEAR_SPEC = MethodSpec(
    name=CLEAR,
    owner="daemon.agents",
    params_schema=_CLEAR_PARAMS,
    result_schema=_MUTATION_RESULT,
    errors=(*_CLEAR_ERRORS, MethodError(ipc_errors.USER_AGENT_IMMUTABLE, description="The named agent is the owner's user agent, which is never administered; no data payload.")),
    mode="unary",
    mutates=True,
    retry="never",
    audience=("local-operator",),
    http_exposed=False,
    confirmation="none",
    idempotency=IdempotencySpec(mode="never", scope="harness-args-clear"),
)
SPECS = (GET_SPEC, VALIDATE_SPEC, SET_SPEC, CLEAR_SPEC)


def _supported_harness(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "harness must be a non-empty string"
        )
    if is_adapter_platform(value):
        raise DaemonRequestError(
            ipc_errors.USE_ADAPTER_COMMAND,
            f"{value} is an adapter; use the agent adapter command instead",
        )
    if value in _UNSUPPORTED:
        raise DaemonRequestError(
            ipc_errors.UNSUPPORTED_CAPABILITY,
            f"{value} uses its own launch contract and cannot store harness args",
        )
    if value not in _SUPPORTED:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"unknown harness {value!r}; expected claude, codex, pi, or agy",
        )
    return value


def _args(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "args must be an array of strings"
        )
    args = tuple(value)
    if any("\x00" in item for item in args):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "args cannot contain a NUL byte"
        )
    if len(args) > 256:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "args exceed the 256 item limit"
        )
    if sum(len(item.encode("utf-8")) for item in args) > 64 * 1024:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "args exceed the 64 KiB UTF-8 limit"
        )
    return args


def _expected(value: object) -> dict[str, tuple[str, ...]]:
    try:
        return normalize_harness_args(value)
    except AgentError as error:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"expectedHarnessArgs is invalid: {error}",
        ) from error


def _dangerous_rows(harness: str, args: tuple[str, ...]) -> list[JsonObject]:
    return [
        {
            "token": item.token,
            "category": item.category,
            "reason": item.reason,
        }
        for item in classify_dangerous_launch_args(harness, args)
    ]


def _issue_rows(error: HarnessArgValidationError) -> list[JsonObject]:
    return [
        {
            "code": issue.code,
            "message": issue.message,
            "token": issue.token,
        }
        for issue in error.issues
    ]


def _preview(harness: str, args: tuple[str, ...]) -> JsonObject:
    dangerous = _dangerous_rows(harness, args)
    try:
        normalized = validate_harness_launch_args(harness, args)
    except HarnessArgValidationError as error:
        return {
            "harness": harness,
            "args": list(args),
            "valid": False,
            "issues": [issue.message for issue in error.issues],
            "warnings": [],
            "dangerous": dangerous,
        }
    return {
        "harness": harness,
        "args": list(normalized),
        "valid": True,
        "issues": [],
        "warnings": [],
        "dangerous": dangerous,
    }


def _operator(app: Any, trusted_message_origin: str | None) -> str:
    operator, _caller_pid = app._identity_binding_operator(
        {}, trusted_message_origin=trusted_message_origin
    )
    if not operator:
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "only the host local operator may change stored harness arguments",
        )
    digest = hashlib.sha256(
        f"uid:{current_uid()}".encode("utf-8")
    ).hexdigest()[:16]
    return f"local-operator:{digest}"


def _current_agent(app: Any, name: str) -> Any:
    try:
        agent = app.agents.current(name)
    except AgentError as error:
        raise DaemonRequestError(error.code, str(error)) from error
    if agent is None:
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND,
            f"no agent named {name!r} on this machine",
        )
    return agent


def _mutation_result(app: Any, event: Any, name: str) -> JsonObject:
    agent = _current_agent(app, name)
    return {
        "ok": True,
        "changed": event.changed,
        "agent": app._agent_status_json(agent),
        "harnessArgs": {
            harness: list(args)
            for harness, args in agent.harness_args.items()
        },
        "auditId": event.audit_id,
    }


def _set_command(
    app: Any,
    *,
    name: str,
    harness: str | None,
    args: tuple[str, ...] | None,
    expected: Mapping[str, tuple[str, ...]],
    operator: str,
    reason: str | None,
    confirmed_dangerous: bool,
    correlation_id: str,
) -> Any:
    from hyprial.daemon.impl.composition import DomainCommandError

    try:
        return app.agents.set_harness_args(
            name,
            harness=harness,
            args=args,
            expected_harness_args=expected,
            operator=operator,
            reason=reason,
            correlation_id=correlation_id,
            confirmed_dangerous=confirmed_dangerous,
        )
    except DomainCommandError as error:
        data = getattr(error, "data", None)
        if error.code == ipc_errors.HARNESS_ARGS_CONFLICT:
            data = {"action": "re-read", "method": GET}
        raise DaemonRequestError(error.code, str(error), data=data) from error


def _name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string"
        )
    return value


def get(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    agent = _current_agent(app, _name(params.get("name")))
    harness_args = {
        harness: list(args)
        for harness, args in agent.harness_args.items()
    }
    return {
        "ok": True,
        "agent": app._agent_status_json(agent),
        "harnessArgs": harness_args,
        "harnesses": [
            _preview(harness, args)
            for harness, args in sorted(agent.harness_args.items())
        ],
    }


def validate(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    del app, trusted_message_origin
    harness = _supported_harness(params.get("harness"))
    args = _args(params.get("args"))
    return {"ok": True, **_preview(harness, args)}


def set(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    operator = _operator(app, trusted_message_origin)
    name = _name(params.get("name"))
    refuse_user_agent_target(app.agents, name, "change the harness args of")
    harness = _supported_harness(params.get("harness"))
    args = _args(params.get("args"))
    expected = _expected(params.get("expectedHarnessArgs"))
    try:
        args = validate_harness_launch_args(harness, args)
    except HarnessArgValidationError as error:
        raise DaemonRequestError(
            ipc_errors.HARNESS_ARGS_INVALID,
            "harness args failed managed launch validation",
            {"harness": harness, "issues": _issue_rows(error)},
        ) from error
    dangerous = _dangerous_rows(harness, args)
    confirmed = params.get("confirmDangerous") is True
    if dangerous and not confirmed:
        raise DaemonRequestError(
            ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED,
            "dangerous harness args require confirmDangerous=true",
            {"harness": harness, "dangerous": dangerous},
        )
    event = _set_command(
        app,
        name=name,
        harness=harness,
        args=args,
        expected=expected,
        operator=operator,
        reason=params.get("reason"),
        confirmed_dangerous=confirmed,
        correlation_id=f"agent.harness-args.set:{uuid.uuid4().hex}",
    )
    return _mutation_result(app, event, name)


def clear(
    app: Any,
    params: Mapping[str, Any],
    *,
    trusted_message_origin: str | None = None,
) -> JsonObject:
    operator = _operator(app, trusted_message_origin)
    name = _name(params.get("name"))
    refuse_user_agent_target(app.agents, name, "change the harness args of")
    raw_harness = params.get("harness")
    has_harness = isinstance(raw_harness, str) and bool(raw_harness)
    clear_all = params.get("all") is True
    if has_harness == clear_all:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "pass exactly one non-empty harness or all=true",
        )
    if clear_all and params.get("confirmAll") is not True:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "clear all requires confirmAll=true",
        )
    expected = _expected(params.get("expectedHarnessArgs"))
    event = _set_command(
        app,
        name=name,
        harness=raw_harness if has_harness else None,
        args=None,
        expected=expected,
        operator=operator,
        reason=params.get("reason"),
        confirmed_dangerous=False,
        correlation_id=f"agent.harness-args.clear:{uuid.uuid4().hex}",
    )
    return _mutation_result(app, event, name)


def _param_error(method: str, error: Any, params: Mapping[str, Any]) -> str:
    path = [str(part) for part in getattr(error, "absolute_path", ())]
    field = path[0] if path else ""
    if getattr(error, "validator", None) == "required":
        missing = [
            name for name in getattr(error, "validator_value", ())
            if name not in params
        ]
        field = missing[0] if missing else field
    if field == "expectedHarnessArgs":
        return (
            "expectedHarnessArgs is required; re-read with "
            "agent.harness-args.get and retry with a current client"
        )
    if field in {"name", "harness"}:
        return f"{field} must be a non-empty string"
    if field == "args":
        return "args must be an array of at most 256 strings"
    location = "/".join(path) or "<root>"
    return f"{method} params do not match the declared schema at {location}"


BINDINGS = (
    (GET_SPEC, get, partial(_param_error, GET)),
    (VALIDATE_SPEC, validate, partial(_param_error, VALIDATE)),
    (SET_SPEC, set, partial(_param_error, SET)),
    (CLEAR_SPEC, clear, partial(_param_error, CLEAR)),
)

__all__ = [
    "BINDINGS",
    "CLEAR",
    "CLEAR_SPEC",
    "GET",
    "GET_SPEC",
    "NAMES",
    "SET",
    "SET_SPEC",
    "SPECS",
    "VALIDATE",
    "VALIDATE_SPEC",
    "clear",
    "get",
    "set",
    "validate",
]
