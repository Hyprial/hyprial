"""Launch-argument spelling tables, managed validation, classification, and redaction.

Split from ``hyprial.kernel.impl.facts.launch_spec`` with zero behavior change
so each module stays well below the 1000-line structural limit while preserving
all public and private symbols on the package facade.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from hyprial.kernel.impl.contracts.daemon import ipc_errors

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
        "--skill": ("--skill", True),
        "--approve": ("--approve", False),
        "--offline": ("--offline", False),
    },
    "agy": {
        "--model": ("--model", True),
        "--mode": ("--mode", True),
        "--effort": ("--effort", True),
        "--add-dir": ("--add-dir", True),
        "--agent": ("--agent", True),
        "--append-system-prompt": ("--append-system-prompt", True),
        "--log-file": ("--log-file", True),
        "--dangerously-skip-permissions": ("--dangerously-skip-permissions", False),
        "--sandbox": ("--sandbox", False),
        "--disable-telemetry": ("--disable-telemetry", False),
        "--include-default-customizations": ("--include-default-customizations", False),
        "--process-per-session": ("--process-per-session", False),
    },
}


@dataclass(frozen=True, slots=True)
class HarnessArgIssue:
    """One safe, actionable reason a managed launch argument was rejected."""

    code: str
    message: str
    token: str | None = None


@dataclass(frozen=True, slots=True)
class DangerousArg:
    """A known or pattern-matched launch argument that requires confirmation."""

    token: str
    category: str
    reason: str


@dataclass(frozen=True, slots=True)
class RedactedHarnessArgs:
    """A redacted canonical argument map plus its pre-redaction digest."""

    harness_args: dict[str, tuple[str, ...]]
    json: str
    digest: str
    truncated: bool


class HarnessArgValidationError(ValueError):
    """Managed launch arguments failed adapter-owned policy validation."""

    code = ipc_errors.HARNESS_ARGS_INVALID
    permanent_start_failure = True
    start_precondition_failure = True
    suppress_retry_alarm = True

    def __init__(self, issues: Sequence[HarnessArgIssue]) -> None:
        normalized = tuple(issues)
        if not normalized:
            raise ValueError("HarnessArgValidationError requires at least one issue")
        self.issues = normalized
        super().__init__("; ".join(issue.message for issue in normalized))


_MANAGED_HARNESS_ARGS = frozenset({"claude", "codex", "pi", "agy"})
_AGY_MODES = frozenset({"default", "accept-edits", "plan"})
_AGY_EFFORTS = frozenset({"low", "medium", "high"})
_MAX_HARNESS_ARG_COUNT = 256
_MAX_HARNESS_ARG_BYTES = 64 * 1024
_MAX_REDACTED_VALUE_CHARS = 512
_MAX_REDACTED_JSON_BYTES = 16 * 1024
_CLAUDE_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)
_CLAUDE_VALUE_OPTIONS = frozenset(
    {
        "--add-dir",
        "--agents",
        "--allowedTools",
        "--allowed-tools",
        "--append-system-prompt",
        "--disallowedTools",
        "--disallowed-tools",
        "--effort",
        "--fallback-model",
        "--input-format",
        "--max-budget-usd",
        "--max-turns",
        "--mcp-config",
        "--model",
        "--output-format",
        "--permission-mode",
        "--plugin-dir",
        "--setting-sources",
        "--settings",
    }
)
_CLAUDE_FLAG_OPTIONS = frozenset(
    {
        "--chrome",
        "--dangerously-load-development-channels",
        "--dangerously-skip-permissions",
        "--debug",
        "--disable-slash-commands",
        "--help",
        "--ide",
        "--include-partial-messages",
        "--no-chrome",
        "--replay-user-messages",
        "--strict-mcp-config",
        "--verbose",
        "--version",
    }
)
_LIFECYCLE_OPTIONS = {
    "claude": frozenset({"--continue", "--fork-session", "--resume", "--session-id"}),
    "codex": frozenset({"--continue", "--resume", "--session-id"}),
    "pi": frozenset(
        {"--continue", "--fork", "--mode", "--print", "--resume", "--session",
         "--session-id", "-c", "-p", "-r"}
    ),
    "agy": frozenset(
        {
            "--app-data-dir",
            "--app_data_dir",
            "--cli_dir_name",
            "--continue",
            "--conversation",
            "--gemini-dir",
            "--gemini_dir",
            "--login",
            "--resume",
            "--stdio",
            "--worker",
            "-c",
        }
    ),
}
_CODEX_VALUE_ALIASES = {
    "--ask-for-approval": "--ask-for-approval",
    "--config": "--config",
    "--model": "--model",
    "--sandbox": "--sandbox",
    "-a": "--ask-for-approval",
    "-c": "--config",
    "-m": "--model",
    "-s": "--sandbox",
}
_CODEX_FLAG_OPTIONS = frozenset(
    {"--approve-for-me", "--dangerously-bypass-approvals-and-sandbox"}
)
_CODEX_APPROVAL_POLICIES = frozenset({"untrusted", "on-request", "never"})
_CODEX_SANDBOXES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_CODEX_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_SENSITIVE_HARNESS_OPTIONS = frozenset(
    {"--api-key", "--authorization", "--env", "--header", "--mcp-config",
     "--password", "--secret", "--settings", "--token"}
)
_DANGEROUS_OPTION_PATTERN = re.compile(
    r"^--(?:dangerously-|yolo$|bypass-|no-.*sandbox|allow-)"
)


def _reject_harness_arg(code: str, message: str, token: str | None = None) -> None:
    raise HarnessArgValidationError((HarnessArgIssue(code, message, token),))


def _safe_option_token(token: str) -> str:
    if not token.startswith("-") or token == "-":
        return "<positional>"
    return token.split("=", 1)[0]


def _checked_harness_arg_group(harness: str, value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        _reject_harness_arg(
            "invalid-args", f"{harness} args must be an array of strings"
        )
    if any(not isinstance(item, str) for item in value):
        _reject_harness_arg(
            "invalid-arg", f"{harness} args must contain only strings"
        )
    args = tuple(value)
    if any("\x00" in item for item in args):
        _reject_harness_arg(
            "nul-byte", f"{harness} args cannot contain a NUL byte"
        )
    if len(args) > _MAX_HARNESS_ARG_COUNT:
        _reject_harness_arg(
            "too-many-args", f"{harness} args exceed the 256 item limit"
        )
    if sum(len(item.encode("utf-8")) for item in args) > _MAX_HARNESS_ARG_BYTES:
        _reject_harness_arg(
            "args-too-large", f"{harness} args exceed the 64 KiB UTF-8 limit"
        )
    return args


def normalize_harness_args(value: object) -> dict[str, tuple[str, ...]]:
    """Normalize a phase-one harness argument map or raise safe policy issues.

    Empty groups are removed, keys are sorted, tuple order is preserved, and
    each group is bounded to 256 strings and 64 KiB of UTF-8 content.
    """

    if not isinstance(value, Mapping):
        _reject_harness_arg(
            "invalid-map", "harness args must be an object keyed by harness"
        )
    if any(not isinstance(harness, str) for harness in value):
        _reject_harness_arg(
            "unsupported-harness", "harness names must be strings"
        )
    normalized: dict[str, tuple[str, ...]] = {}
    for harness in sorted(value):
        if not isinstance(harness, str) or harness not in _MANAGED_HARNESS_ARGS:
            _reject_harness_arg(
                "unsupported-harness",
                f"harness {harness!r} has no phase-one managed argument policy",
            )
        args = _checked_harness_arg_group(harness, value[harness])
        if args:
            normalized[harness] = args
    return normalized


def _split_policy_option(token: str) -> tuple[str, str | None]:
    if token.startswith("-c") and not token.startswith("--") and token != "-c":
        return "-c", token[2:]
    key, separator, inline = token.partition("=")
    return key, inline if separator else None


def _required_option_value(
    args: tuple[str, ...], index: int, key: str, inline: str | None
) -> tuple[str, int]:
    if inline is not None:
        if inline:
            return inline, index + 1
        _reject_harness_arg("missing-option-value", f"{key} requires a value", key)
    if index + 1 >= len(args) or args[index + 1].startswith("-"):
        _reject_harness_arg("missing-option-value", f"{key} requires a value", key)
    return args[index + 1], index + 2


def _reject_lifecycle_option(harness: str, key: str) -> None:
    _reject_harness_arg(
        "managed-lifecycle-conflict",
        f"{key} is owned by the daemon {harness} lifecycle; remove it from stored args",
        key,
    )


def _reject_unsupported_option(harness: str, key: str) -> None:
    _reject_harness_arg(
        "unsupported-option",
        f"{key} is not supported by {harness} managed launch; remove it or update the policy",
        key,
    )


def _validate_claude_launch_args(args: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-") or token == "-":
            _reject_harness_arg(
                "positional-not-supported",
                "Claude managed launch does not accept stored prompts or subcommands",
                "<positional>",
            )
        key, inline = _split_policy_option(token)
        if key in _LIFECYCLE_OPTIONS["claude"]:
            _reject_lifecycle_option("Claude", key)
        if key == "--json":
            _reject_harness_arg(
                "unsupported-option",
                "--json is not a Claude managed launch option; remove it",
                key,
            )
        if key in _CLAUDE_VALUE_OPTIONS:
            value, index = _required_option_value(args, index, key, inline)
            if key == "--permission-mode" and value not in _CLAUDE_PERMISSION_MODES:
                _reject_harness_arg(
                    "invalid-option-value",
                    "--permission-mode must use a managed Claude permission mode",
                    key,
                )
            if key == "--effort" and value not in {"low", "medium", "high", "xhigh", "max"}:
                _reject_harness_arg(
                    "invalid-option-value",
                    "--effort must use a supported Claude effort level",
                    key,
                )
            continue
        if key in _CLAUDE_FLAG_OPTIONS:
            if inline is not None and key != "--debug":
                _reject_harness_arg(
                    "invalid-option-value", f"{key} does not take a value", key
                )
            index += 1
            continue
        _reject_unsupported_option("Claude", key)
    return args


def _codex_effort_from_config(value: str, key: str) -> str:
    name, separator, raw = value.partition("=")
    if not separator or name.strip() != "model_reasoning_effort":
        _reject_unsupported_option("Codex", key)
    effort = raw.strip().strip("\"'")
    if effort not in _CODEX_EFFORTS:
        _reject_harness_arg(
            "invalid-option-value",
            "model_reasoning_effort must use a supported Codex effort level",
            key,
        )
    return effort


def _validate_codex_launch_args(args: tuple[str, ...]) -> tuple[str, ...]:
    parsed: dict[str, str] = {}
    flags: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-") or token == "-":
            _reject_harness_arg(
                "positional-not-supported",
                "Codex app-server launch does not accept stored prompts or subcommands",
                "<positional>",
            )
        key, inline = _split_policy_option(token)
        if key in _LIFECYCLE_OPTIONS["codex"]:
            _reject_lifecycle_option("Codex", key)
        if key in _CODEX_FLAG_OPTIONS:
            if inline is not None:
                _reject_harness_arg(
                    "invalid-option-value", f"{key} does not take a value", key
                )
            flags.add(key)
            index += 1
            continue
        canonical = _CODEX_VALUE_ALIASES.get(key)
        if canonical is None:
            _reject_unsupported_option("Codex", key)
        value, index = _required_option_value(args, index, key, inline)
        if canonical in parsed:
            _reject_harness_arg(
                "conflicting-options",
                f"{canonical} must appear at most once for Codex app-server",
                canonical,
            )
        parsed[canonical] = value
    if "--config" in parsed:
        _codex_effort_from_config(parsed["--config"], "--config")
    approval = parsed.get("--ask-for-approval")
    if approval is not None and approval not in _CODEX_APPROVAL_POLICIES:
        _reject_harness_arg(
            "invalid-option-value",
            "--ask-for-approval must use a Codex app-server approval policy",
            "--ask-for-approval",
        )
    sandbox = parsed.get("--sandbox")
    if sandbox is not None and sandbox not in _CODEX_SANDBOXES:
        _reject_harness_arg(
            "invalid-option-value",
            "--sandbox must use a Codex app-server sandbox mode",
            "--sandbox",
        )
    if "--approve-for-me" in flags and approval is not None:
        _reject_harness_arg(
            "conflicting-options",
            "--approve-for-me cannot be combined with --ask-for-approval",
            "--approve-for-me",
        )
    bypass = "--dangerously-bypass-approvals-and-sandbox"
    if bypass in flags and (approval is not None or sandbox is not None):
        _reject_harness_arg(
            "conflicting-options",
            f"{bypass} cannot be combined with approval or sandbox overrides",
            bypass,
        )
    return args


def _validate_pi_launch_args(args: tuple[str, ...]) -> tuple[str, ...]:
    table = _HARNESS_ARG_OPTIONS["pi"]
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-") or token == "-":
            _reject_harness_arg(
                "positional-not-supported",
                "Pi managed launch does not accept stored prompts or subcommands",
                "<positional>",
            )
        key, inline = _split_policy_option(token)
        if key in _LIFECYCLE_OPTIONS["pi"]:
            _reject_lifecycle_option("Pi", key)
        option = table.get(key)
        if option is None:
            _reject_unsupported_option("Pi", key)
        if option[1]:
            _value, index = _required_option_value(args, index, key, inline)
            continue
        if inline is not None:
            _reject_harness_arg(
                "invalid-option-value", f"{key} does not take a value", key
            )
        index += 1
    return args


def _validate_agy_launch_args(args: tuple[str, ...]) -> tuple[str, ...]:
    table = _HARNESS_ARG_OPTIONS["agy"]
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-") or token == "-":
            _reject_harness_arg(
                "positional-not-supported",
                "Agy managed launch does not accept stored prompts or subcommands",
                "<positional>",
            )
        key, inline = _split_policy_option(token)
        if key in _LIFECYCLE_OPTIONS["agy"]:
            _reject_lifecycle_option("Agy", key)
        option = table.get(key)
        if option is None:
            _reject_unsupported_option("Agy", key)
        if option[1]:
            value, index = _required_option_value(args, index, key, inline)
            if key == "--mode" and value not in _AGY_MODES:
                _reject_harness_arg(
                    "invalid-option-value",
                    "--mode must be one of: default, accept-edits, plan",
                    key,
                )
            if key == "--effort" and value not in _AGY_EFFORTS:
                _reject_harness_arg(
                    "invalid-option-value",
                    "--effort must use a supported Agy effort level (low, medium, high)",
                    key,
                )
            continue
        if inline is not None:
            _reject_harness_arg(
                "invalid-option-value", f"{key} does not take a value", key
            )
        index += 1
    return args


def managed_harness_args_supported(harness: object) -> bool:
    """Managed harnesses own argv; others (test doubles) pass theirs through."""
    return isinstance(harness, str) and harness in _MANAGED_HARNESS_ARGS


def validate_harness_launch_args(harness: str, args: tuple[str, ...]) -> tuple[str, ...]:
    """Validate and return phase-one managed argv without exposing raw values.

    Validation issues contain stable codes, safe option names, and actionable
    messages. Positionals and daemon-owned lifecycle controls are rejected.
    """

    if harness not in _MANAGED_HARNESS_ARGS:
        _reject_harness_arg(
            "unsupported-harness",
            f"harness {harness!r} has no phase-one managed argument policy",
        )
    checked = _checked_harness_arg_group(harness, args)
    if harness == "claude":
        return _validate_claude_launch_args(checked)
    if harness == "codex":
        return _validate_codex_launch_args(checked)
    if harness == "agy":
        return _validate_agy_launch_args(checked)
    return _validate_pi_launch_args(checked)


def _classification_value(args: tuple[str, ...], index: int, inline: str | None) -> str | None:
    if inline is not None:
        return inline
    if index + 1 < len(args) and not args[index + 1].startswith("-"):
        return args[index + 1]
    return None


def _dangerous_arg(token: str, category: str, reason: str) -> DangerousArg:
    return DangerousArg(token=token, category=category, reason=reason)


def classify_dangerous_launch_args(harness: str, args: tuple[str, ...]) -> tuple[DangerousArg, ...]:
    """Classify known and suspicious launch options without echoing values."""

    dangerous: list[DangerousArg] = []
    known_pattern_keys = {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
        "--yolo",
    }
    for index, token in enumerate(args):
        if not isinstance(token, str) or not token.startswith("-"):
            continue
        key, inline = _split_policy_option(token)
        value = _classification_value(args, index, inline)
        if harness == "claude" and (
            key == "--dangerously-skip-permissions"
            or key == "--permission-mode" and value == "bypassPermissions"
        ):
            dangerous.append(
                _dangerous_arg(
                    key, "privilege-escalation", "bypasses Claude permissions"
                )
            )
        if harness == "codex" and key in {
            "--dangerously-bypass-approvals-and-sandbox",
            "--yolo",
        }:
            dangerous.append(
                _dangerous_arg(
                    key, "privilege-escalation", "bypasses Codex isolation"
                )
            )
        if (
            harness == "codex"
            and (key == "--approve-for-me" or key == "--ask-for-approval" and value == "never")
        ) or harness == "pi" and key == "--approve":
            dangerous.append(
                _dangerous_arg(
                    key, "approval-bypass", "removes or automates launch approval"
                )
            )
        if (
            harness == "codex"
            and key == "--sandbox"
            and value == "danger-full-access"
        ):
            dangerous.append(
                _dangerous_arg(
                    key, "sandbox-bypass", "selects unrestricted sandbox access"
                )
            )
        if harness == "claude" and key in {
            "--mcp-config",
            "--plugin-dir",
            "--settings",
        }:
            dangerous.append(
                _dangerous_arg(
                    key, "code-execution", "loads executable or trusted configuration"
                )
            )
        if harness == "agy" and key == "--dangerously-skip-permissions":
            dangerous.append(
                _dangerous_arg(
                    key, "privilege-escalation", "bypasses Antigravity tool permissions"
                )
            )
        if harness == "agy" and key == "--mode" and value == "auto":
            dangerous.append(
                _dangerous_arg(
                    key, "approval-bypass", "enables auto-approval mode"
                )
            )
        if _DANGEROUS_OPTION_PATTERN.match(key) and key not in known_pattern_keys:
            dangerous.append(
                _dangerous_arg(
                    key,
                    "unknown-dangerous-pattern",
                    "matches a dangerous option shape not registered by the adapter",
                )
            )
    return tuple(dangerous)


def _truncate_redacted_token(token: str) -> tuple[str, bool]:
    key, separator, value = token.partition("=")
    if separator and key in _SENSITIVE_HARNESS_OPTIONS:
        return f"{key}=<redacted>", False
    if separator and len(value) > _MAX_REDACTED_VALUE_CHARS:
        return f"{key}={value[:_MAX_REDACTED_VALUE_CHARS]}", True
    if len(token) > _MAX_REDACTED_VALUE_CHARS:
        return token[:_MAX_REDACTED_VALUE_CHARS], True
    return token, False


def _redact_harness_arg_group(args: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    redacted: list[str] = []
    truncated = False
    index = 0
    while index < len(args):
        token = args[index]
        key = _safe_option_token(token)
        if key in _SENSITIVE_HARNESS_OPTIONS and "=" not in token:
            redacted.append(key)
            if index + 1 < len(args):
                redacted.append("<redacted>")
                index += 2
            else:
                index += 1
            continue
        safe, shortened = _truncate_redacted_token(token)
        redacted.append(safe)
        truncated = truncated or shortened
        index += 1
    return tuple(redacted), truncated


def _canonical_harness_args_json(value: Mapping[str, Sequence[str]]) -> str:
    return json.dumps(
        {key: list(value[key]) for key in sorted(value)},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalize_redaction_input(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        return normalize_harness_args(value)
    if any(not isinstance(harness, str) or not harness for harness in value):
        _reject_harness_arg(
            "unsupported-harness", "harness names must be non-empty strings"
        )
    normalized: dict[str, tuple[str, ...]] = {}
    for harness in sorted(value):
        raw_args = value[harness]
        if not isinstance(raw_args, (list, tuple)) or any(not isinstance(item, str) for item in raw_args):
            _reject_harness_arg("invalid-args", f"{harness} args must be an array of strings")
        args = tuple(raw_args)
        if args:
            normalized[harness] = args
    return normalized


def redact_harness_args(value: object) -> RedactedHarnessArgs:
    """Redact a canonical argument map and hash the complete pre-redaction map.

    Sensitive option values are replaced, other values are limited to 512
    characters, and oversized audit JSON becomes a valid summary below 16 KiB.
    """

    normalized = _normalize_redaction_input(value)
    raw_json = _canonical_harness_args_json(normalized)
    digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    redacted: dict[str, tuple[str, ...]] = {}
    truncated = False
    for harness, args in normalized.items():
        safe, shortened = _redact_harness_arg_group(args)
        redacted[harness] = safe
        truncated = truncated or shortened
    audit_json = _canonical_harness_args_json(redacted)
    if len(audit_json.encode("utf-8")) > _MAX_REDACTED_JSON_BYTES:
        audit_json = json.dumps(
            {
                "_truncated": True,
                "argCounts": {
                    harness: len(args) for harness, args in redacted.items()
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        truncated = True
    return RedactedHarnessArgs(
        harness_args=redacted,
        json=audit_json,
        digest=digest,
        truncated=truncated,
    )
