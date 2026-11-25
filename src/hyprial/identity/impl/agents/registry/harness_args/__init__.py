"""Atomic persistent harness-argument mutation and append-only audit."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from hyprial.kernel import (
    DangerousArg,
    HarnessArgValidationError,
    classify_dangerous_launch_args,
    ipc_errors,
    redact_harness_args,
    validate_harness_launch_args,
)

from .._base import Agent, AgentError, AgentNotFoundError, normalize_harness_args


class HarnessArgsConflict(AgentError):
    code = ipc_errors.HARNESS_ARGS_CONFLICT


class HarnessArgsInvalid(AgentError):
    code = ipc_errors.HARNESS_ARGS_INVALID


class HarnessArgsDangerousConfirmationRequired(AgentError):
    code = ipc_errors.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED


@dataclass(frozen=True, slots=True)
class HarnessArgsMutation:
    agent: Agent
    changed: bool
    audit_id: str | None


def _reason(value: str | None) -> str | None:
    if value is None:
        return None
    return "".join(
        character for character in value
        if ord(character) >= 32 and ord(character) != 127
    )[:512]


def _dangerous_json(dangerous: tuple[DangerousArg, ...]) -> str:
    return json.dumps(
        [
            {
                "token": item.token,
                "category": item.category,
                "reason": item.reason,
            }
            for item in dangerous
        ],
        separators=(",", ":"),
        sort_keys=True,
    )


class _RegistryHarnessArgsMixin:
    def set_harness_args(
        self,
        actor: str,
        *,
        harness: str | None,
        args: tuple[str, ...] | None,
        expected_harness_args: object,
        operator: str,
        reason: str | None,
        correlation_id: str,
        confirmed_dangerous: bool,
    ) -> HarnessArgsMutation:
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must be a non-empty trusted identity")
        if not isinstance(correlation_id, str) or not correlation_id.strip():
            raise ValueError("correlation_id must be non-empty")
        if harness is None and args is not None:
            raise ValueError("clear-all requires args=None")
        if harness is not None and (not isinstance(harness, str) or not harness):
            raise ValueError("harness must be a non-empty string")
        expected = normalize_harness_args(expected_harness_args)
        desired_args = None if args is None else tuple(args)
        dangerous = ()
        name = self.actor_key(actor)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT * FROM agents WHERE actor = ?", (name,)
                ).fetchone()
                if row is None:
                    raise AgentNotFoundError(f"no agent named {actor!r}")
                current = normalize_harness_args(json.loads(row["harness_args"]))
                if current != expected:
                    raise HarnessArgsConflict(
                        "harnessArgs changed since read; re-read before writing"
                    )
                if harness is not None and desired_args:
                    try:
                        desired_args = validate_harness_launch_args(harness, desired_args)
                    except HarnessArgValidationError as error:
                        raise HarnessArgsInvalid(str(error)) from error
                    dangerous = classify_dangerous_launch_args(harness, desired_args)
                    if dangerous and not confirmed_dangerous:
                        tokens = ", ".join(item.token for item in dangerous)
                        raise HarnessArgsDangerousConfirmationRequired(
                            f"dangerous harness args require confirmation: {tokens}"
                        )
                replacement = dict(current)
                if harness is None:
                    replacement.clear()
                    action = "clear-all"
                elif desired_args:
                    replacement[harness] = desired_args
                    action = "set"
                else:
                    replacement.pop(harness, None)
                    action = "clear"
                replacement = normalize_harness_args(replacement)
                if replacement == current:
                    self._db.commit()
                    return HarnessArgsMutation(
                        self._row_agent(row),
                        False,
                        None,
                    )
                before = redact_harness_args(current)
                after = redact_harness_args(replacement)
                updated = self._db.execute(
                    "UPDATE agents SET harness_args = ? "
                    "WHERE actor = ? AND entity_token = ?",
                    (
                        json.dumps(
                            {key: list(value) for key, value in replacement.items()},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        name,
                        row["entity_token"],
                    ),
                )
                if updated.rowcount != 1:
                    raise AgentError("agent incarnation changed during harness args update")
                audit = self._db.execute(
                    "INSERT INTO agent_harness_args_audit "
                    "(at_ms, actor, entity_token, action, harness, before_json, "
                    "after_json, before_digest, after_digest, dangerous_json, "
                    "confirmed_dangerous, operator, reason, correlation_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._clock(), name, row["entity_token"], action, harness,
                        before.json, after.json, before.digest, after.digest,
                        _dangerous_json(dangerous), int(confirmed_dangerous),
                        operator, _reason(reason), correlation_id,
                    ),
                )
                audit_id = f"agent_harness_args_audit:{audit.lastrowid}"
                self._db.commit()
            except BaseException as error:
                self._db.rollback()
                if isinstance(error, sqlite3.Error):
                    raise AgentError(
                        f"harness args transaction failed: {error}"
                    ) from error
                raise
            refreshed = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if refreshed is None:
                raise AgentError("agent disappeared after harness args commit")
            return HarnessArgsMutation(
                self._row_agent(
                    refreshed
                ),
                True,
                audit_id,
            )


__all__ = [
    "HarnessArgsConflict",
    "HarnessArgsDangerousConfirmationRequired",
    "HarnessArgsInvalid",
    "HarnessArgsMutation",
    "_RegistryHarnessArgsMixin",
]
