"""Launch-state vocabulary: HarnessLaunchSpec + DesiredStateError (canonical).

依据 PY-BATCH-2026-10-01 任务书 §2.1：HarnessLaunchSpec 与 DesiredStateError 的唯一
canonical 定义；_HARNESSES 与三个无状态私有校验 helper 按许可原文复制（daemon 侧
desired_state 保留本地私有副本，避免扩大公共 API）。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Any, Self

from hyprial.kernel.impl.contracts.execution.execution_runtime import (
    SmolvmRuntimeSpec,
    parse_execution_runtime,
)


# Worker kinds are shared vocabulary, independent of daemon capabilities.
# Adapter migration belongs to the adapter package, not this vocabulary.
DECLARED_HARNESSES = frozenset({"codex", "claude", "pi", "dsh", "jev", "user-proxy"})
_HARNESSES = DECLARED_HARNESSES


# Known argument spellings per harness. Each entry maps a spelling to
# ``(canonical option key, takes_value)`` so an alias pair (codex ``-a`` and
# ``--ask-for-approval``) is one option, and a known flag never swallows the
# positional argument that follows it. Options not listed here fall back to
# the generic rule: a following non-option token is the option's value.
_HARNESS_ARG_OPTIONS: dict[str, dict[str, tuple[str, bool]]] = {
    "codex": {
        "--model": ("--model", True),
        "-m": ("--model", True),
        "--ask-for-approval": ("--ask-for-approval", True),
        "-a": ("--ask-for-approval", True),
        "--sandbox": ("--sandbox", True),
        "-s": ("--sandbox", True),
        "--approve-for-me": ("--approve-for-me", False),
        "--dangerously-bypass-approvals-and-sandbox": (
            "--dangerously-bypass-approvals-and-sandbox",
            False,
        ),
    },
    "pi": {
        "--provider": ("--provider", True),
        "--model": ("--model", True),
        "--mode": ("--mode", True),
        "--session-id": ("--session-id", True),
        "--session": ("--session", True),
        "--fork": ("--fork", True),
        "--print": ("--print", False),
        "-p": ("--print", False),
        "--continue": ("--continue", False),
        "-c": ("--continue", False),
        "--resume": ("--resume", False),
        "-r": ("--resume", False),
        "--approve": ("--approve", False),
        "--offline": ("--offline", False),
    },
}


class DesiredStateError(ValueError):
    pass


def merge_harness_launch_args(
    stored: "tuple[str, ...] | list[str]",
    explicit: "tuple[str, ...] | list[str]",
    *,
    harness: str | None = None,
) -> tuple[str, ...]:
    """Merge an agent's stored harnessArgs with this launch's explicit args.

    The stored value is durable intent (``Agent.harness_args``); the explicit
    value is what this particular start/restore/wake asked for.  The explicit
    value wins per option group: an option the caller names explicitly
    suppresses the stored spelling of the same option (``--name value``,
    ``--name=value`` and, for a known harness, an alias such as codex ``-a``),
    and every remaining stored group is appended unless the identical group is
    already present.  Tokens that belong to different options are never
    dropped merely because they share a value.  ``harness`` selects the known
    option table; unknown options use the generic option/value rule.
    """

    stored_args = tuple(stored)
    explicit_args = tuple(explicit)
    if not stored_args:
        return explicit_args
    if not explicit_args:
        return stored_args
    stored_groups = _launch_arg_groups(stored_args, harness)
    explicit_groups = _launch_arg_groups(explicit_args, harness)
    explicit_keys = {key for key, _tokens in explicit_groups if key is not None}
    merged = list(explicit_groups)
    seen = {tokens for _key, tokens in explicit_groups}
    for key, tokens in stored_groups:
        if key is not None and key in explicit_keys:
            continue
        if tokens in seen:
            continue
        merged.append((key, tokens))
        seen.add(tokens)
    return tuple(token for _key, tokens in merged for token in tokens)


def _launch_option_key(token: str) -> str | None:
    """The option spelling of ``token``, long or short, or None."""

    if token == "--" or not token.startswith("-") or token == "-":
        return None
    return token.split("=", 1)[0]


def _launch_arg_groups(
    args: tuple[str, ...], harness: str | None
) -> list[tuple[str | None, tuple[str, ...]]]:
    """Group tokens: an option that is followed by a value owns that value."""

    table = _HARNESS_ARG_OPTIONS.get(harness or "", {})
    groups: list[tuple[str | None, tuple[str, ...]]] = []
    index = 0
    while index < len(args):
        token = args[index]
        key = _launch_option_key(token)
        if key is None:
            groups.append((None, (token,)))
            index += 1
            continue
        known = table.get(key)
        canonical = known[0] if known is not None else key
        if "=" in token:
            groups.append((canonical, (token,)))
            index += 1
            continue
        has_next = index + 1 < len(args)
        next_is_value = has_next and _launch_option_key(args[index + 1]) is None
        if known is not None and known[1]:
            consumes = has_next
        elif known is not None:
            consumes = False
        else:
            consumes = next_is_value
        if consumes:
            groups.append((canonical, (token, args[index + 1])))
            index += 2
        else:
            groups.append((canonical, (token,)))
            index += 1
    return groups


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise DesiredStateError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DesiredStateError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)



@dataclass(frozen=True, slots=True)
class HarnessLaunchSpec:
    harness: str
    name: str
    headless: bool
    args: tuple[str, ...] = ()
    ownership: str = "managed"
    nickname: str | None = None
    cwd: str | None = None
    endpoint: str | None = None
    session_ref: str | None = None
    # Absolute harness binary resolved at registration time (postmortem
    # 2026-08-23: a daemon launched by launchd/cron has a minimal PATH, so a
    # bare binary name is a restart-context-dependent accident — codex under
    # ~/.local/bin was unstartable exactly then).  Empty means a legacy entry
    # registered before this field existed; ``resolved_command`` then tries
    # the current PATH and finally falls back to the bare default.
    command: tuple[str, ...] = ()
    # RETIRED (#277: no timeout ever kills a turn).  Parsed, validated and
    # round-tripped so existing desired-state files and older daemons keep
    # working across the version boundary, but nothing enforces it.
    turn_timeout_seconds: float | None = None
    # Quiet-period REPORT sensitivity (worker.turn.stalled /
    # worker.turn.resumed; codex only): how long a turn may go without new
    # correlated harness activity before it is reported -- never killed.
    # None defers to HYPRIAL_TURN_IDLE_TIMEOUT_SECONDS, then the harness
    # default; 0 disables reporting.
    idle_timeout_seconds: float | None = None

    def resolved_command(self, default: tuple[str, ...]) -> tuple[str, ...]:
        """The command to spawn: persisted absolute path, else a live PATH
        resolution (legacy entries), else the bare default unchanged."""

        if self.command:
            return self.command
        found = shutil.which(default[0])
        if found is None:
            return default
        # abspath, not resolve(): vendor entry points like ~/.local/bin/codex
        # are symlinks their own updaters repoint; freezing the resolved
        # target would rot on the next upgrade.
        return (os.path.abspath(found), *default[1:])
    containerized: bool = False
    pinned_owner: str | None = None
    container_image: str | None = None
    execution_runtime: SmolvmRuntimeSpec | None = None
    # Model-vendor selection is separate from ``harness``.  The on-disk key
    # ``provider`` remains the harness for schema-v1 downgrade compatibility;
    # these fields use unambiguous names and never contain credentials.
    model_provider: str | None = None
    model: str | None = None
    # U0b (Allen 2026-09-03): every desired-state row is "user intent +
    # LAST KNOWN RESULT".  ``running`` is the default and is OMITTED from
    # the serialized form (absent == running, exactly like every other
    # optional field), so documents written before this column existed
    # parse and re-serialize byte-identically.  ``failed`` means the harness
    # ran successfully at some point and its most recent (re)start did not
    # come up: displayed for a human, never auto-retried.  A start that
    # never succeeded writes NO row at all -- the value lives here only for
    # rows that earned their place.
    status: str = "running"

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        # Dual-read: the on-disk key stays "provider" (schemaVersion=1, so a
        # downgrade can still read files this build writes), but a "harness"
        # key is accepted and wins when both are present.
        raw_harness = record.get("harness") or record.get("provider")
        harness = "claude" if raw_harness == "cc" else raw_harness
        if harness not in _HARNESSES:
            raise DesiredStateError(
                f"{label} has an invalid harness; expected codex, claude, pi, dsh, jev, or user-proxy"
            )
        name = _string(record.get("name"), f"{label}.name")
        raw_args = record.get("args", [])
        if not isinstance(raw_args, list) or any(
            not isinstance(item, str) for item in raw_args
        ):
            raise DesiredStateError(f"{label}.args must be an array of strings")
        raw_command = record.get("command", [])
        if not isinstance(raw_command, list) or any(
            not isinstance(item, str) or not item for item in raw_command
        ):
            raise DesiredStateError(
                f"{label}.command must be an array of non-empty strings"
            )
        if raw_command and not os.path.isabs(raw_command[0]):
            # The whole point of persisting the command is independence from
            # the launching daemon's PATH; a relative entry would be a lie.
            raise DesiredStateError(
                f"{label}.command[0] must be an absolute path"
            )
        ownership = record.get("ownership", "managed")
        if ownership != "managed":
            raise DesiredStateError(
                f"{label}.ownership must be managed in desired state"
            )
        headless = bool(record.get("headless"))
        if not headless:
            raise DesiredStateError(
                f"{label} must be headless; interactive sessions use interactiveSessions"
            )
        raw_turn_timeout = record.get("turnTimeoutSeconds")
        if raw_turn_timeout is not None and (
            not isinstance(raw_turn_timeout, (int, float))
            or isinstance(raw_turn_timeout, bool)
            or raw_turn_timeout < 0
        ):
            raise DesiredStateError(
                f"{label}.turnTimeoutSeconds must be a non-negative number "
                "(0 disables the cap)"
            )
        raw_idle_timeout = record.get("idleTimeoutSeconds")
        if raw_idle_timeout is not None and (
            not isinstance(raw_idle_timeout, (int, float))
            or isinstance(raw_idle_timeout, bool)
            or raw_idle_timeout < 0
        ):
            raise DesiredStateError(
                f"{label}.idleTimeoutSeconds must be a non-negative number "
                "(0 disables the idle lease)"
            )
        containerized_raw = record.get("containerized", False)
        if not isinstance(containerized_raw, bool):
            raise DesiredStateError(f"{label}.containerized must be a boolean")
        pinned_owner = _optional_string(
            record.get("pinnedOwner"), f"{label}.pinnedOwner"
        )
        if pinned_owner is not None and ":" in pinned_owner:
            raise DesiredStateError(f"{label}.pinnedOwner must not contain ':'")
        runtime = parse_execution_runtime(record.get("executionRuntime"))
        if runtime is not None and (harness != "codex" or containerized_raw or record.get("containerImage") or record.get("endpoint")):
            raise DesiredStateError("smolvm requires headless Codex without Docker/endpoint")
        raw_status = record.get("status", "running")
        if raw_status not in ("running", "failed"):
            raise DesiredStateError(
                f"{label}.status must be 'running' or 'failed'",
            )
        return cls(
            harness=str(harness),
            name=name,
            headless=headless,
            args=tuple(raw_args),
            ownership="managed",
            nickname=_optional_string(record.get("nickname"), f"{label}.nickname"),
            cwd=_optional_string(record.get("cwd"), f"{label}.cwd"),
            endpoint=_optional_string(record.get("endpoint"), f"{label}.endpoint"),
            session_ref=_optional_string(
                record.get("sessionRef"), f"{label}.sessionRef"
            ),
            command=tuple(raw_command),
            turn_timeout_seconds=(
                None if raw_turn_timeout is None else float(raw_turn_timeout)
            ),
            idle_timeout_seconds=(
                None if raw_idle_timeout is None else float(raw_idle_timeout)
            ),
            containerized=containerized_raw,
            execution_runtime=runtime,
            pinned_owner=pinned_owner,
            container_image=_optional_string(
                record.get("containerImage"), f"{label}.containerImage"
            ),
            model_provider=_optional_string(
                record.get("modelProvider"), f"{label}.modelProvider"
            ),
            model=_optional_string(record.get("model"), f"{label}.model"),
            status=str(raw_status),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "provider": self.harness,
            "name": self.name,
            "headless": self.headless,
            "args": list(self.args),
            "ownership": self.ownership,
            **({"nickname": self.nickname} if self.nickname is not None else {}),
            **({"cwd": self.cwd} if self.cwd is not None else {}),
            **({"endpoint": self.endpoint} if self.endpoint is not None else {}),
            **(
                {"sessionRef": self.session_ref} if self.session_ref is not None else {}
            ),
            **({"command": list(self.command)} if self.command else {}),
            **(
                {"turnTimeoutSeconds": self.turn_timeout_seconds}
                if self.turn_timeout_seconds is not None
                else {}
            ),
            **(
                {"idleTimeoutSeconds": self.idle_timeout_seconds}
                if self.idle_timeout_seconds is not None
                else {}
            ),
            **({"containerized": True} if self.containerized else {}),
            **({"executionRuntime": self.execution_runtime.to_json()} if self.execution_runtime is not None else {}),
            **(
                {"pinnedOwner": self.pinned_owner}
                if self.pinned_owner is not None
                else {}
            ),
            **(
                {"containerImage": self.container_image}
                if self.container_image is not None
                else {}
            ),
            **(
                {"modelProvider": self.model_provider}
                if self.model_provider is not None
                else {}
            ),
            **({"model": self.model} if self.model is not None else {}),
            # Omitted when running: absent == running keeps pre-U0b
            # documents byte-stable across the round trip.
            **({"status": self.status} if self.status != "running" else {}),
        }
