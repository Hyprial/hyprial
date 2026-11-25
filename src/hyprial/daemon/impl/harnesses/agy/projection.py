# Decision record (file header; no inline comments by repository rule):
# - ACP mcpServers requires env as a list of {"name": k, "value": v} dicts and
#   omits WorkerChannel's "type": "stdio" key.
# - --mode auto is explicitly rejected (never silently downgraded) because ACP
#   supports only default, accept-edits, and plan; --mode plan is never injected
#   implicitly.
# - In non-skip-permissions mode, session/request_permission auto-allows only
#   the exact registered Harness Bridge MCP tool names and rejects all others.
"""Launch projection, MCP conversion, config validation, and permission policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.harnesses.agy.protocol import AgyStartPreconditionError
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel
from hyprial.kernel import HarnessLaunchSpec

ACP_MCP_SERVER_NAME = "harness-bridge"
ACP_SUPPORTED_MODES = frozenset({"default", "accept-edits", "plan"})
ACP_GOOGLE_PROVIDER = "google"


@dataclass(frozen=True, slots=True)
class AgyLaunchProjection:
    """Resolved CLI argv and ACP session configuration for one agy launch."""

    argv: tuple[str, ...]
    model: str | None
    thought_level: str | None
    mode: str | None
    system_prompt_append: str | None
    dangerously_skip_permissions: bool
    mcp_servers: tuple[dict[str, object], ...]
    allowed_mcp_tools: frozenset[str]


def worker_channel_to_acp_mcp_server(channel: WorkerChannel) -> dict[str, object]:
    """Convert WorkerChannel.mcp_server into an ACP mcpServers entry."""

    raw = channel.mcp_server
    command = raw.get("command")
    if not isinstance(command, str) or not command:
        raise AgyStartPreconditionError("worker channel mcp_server missing command")
    raw_args = raw.get("args", ())
    args = [str(item) for item in raw_args] if isinstance(raw_args, Sequence) else []
    raw_env = raw.get("env", {})
    env_list: list[dict[str, str]] = []
    if isinstance(raw_env, Mapping):
        for key, value in sorted(raw_env.items()):
            env_list.append({"name": str(key), "value": str(value)})
    return {
        "name": ACP_MCP_SERVER_NAME,
        "command": command,
        "args": args,
        "env": env_list,
    }


def allowed_harness_bridge_tool_names(channel: WorkerChannel | None) -> frozenset[str]:
    """Derive exact ACP tool identifiers for the channel's registered MCP tools."""

    if channel is None:
        return frozenset()
    prefix = f"mcp__{ACP_MCP_SERVER_NAME}__"
    allowed: set[str] = set()
    for entry in channel.allowed_tools:
        if not entry.startswith(prefix):
            continue
        tool_name = entry[len(prefix) :]
        if not tool_name:
            continue
        allowed.add(f"mcp_{ACP_MCP_SERVER_NAME}_{tool_name}")
    return frozenset(allowed)


def select_permission_option(
    params: Mapping[str, object],
    *,
    allow_all: bool,
    allowed_mcp_tools: frozenset[str],
) -> dict[str, object]:
    """Select an ACP session/request_permission outcome according to policy."""

    tool_call = params.get("toolCall")
    tool_name: str | None = None
    if isinstance(tool_call, Mapping):
        raw_name = tool_call.get("name") or tool_call.get("toolName")
        if isinstance(raw_name, str) and raw_name:
            tool_name = raw_name
    should_allow = allow_all or (tool_name is not None and tool_name in allowed_mcp_tools)
    options = params.get("options")
    if not isinstance(options, list):
        return {"outcome": {"outcome": "cancelled"}}
    preferred_kinds = (
        ("allow_once", "allow_always")
        if should_allow
        else ("reject_once", "reject_always")
    )
    for target_kind in preferred_kinds:
        for option in options:
            if not isinstance(option, Mapping):
                continue
            if option.get("kind") == target_kind and isinstance(option.get("optionId"), str):
                return {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": option["optionId"],
                    }
                }
    return {"outcome": {"outcome": "cancelled"}}


def _worker_system_prompt(channel: WorkerChannel) -> str:
    return (
        f"You are managed Hyprial worker {channel.actor}. "
        "Read pending Harness messages with harness_read, reply to requests with "
        "harness_reply, acknowledge terminal messages with harness_ack, and use "
        "workflow_complete or workflow_fail for PAC workflow nodes."
    )


def _split_option(
    args: Sequence[str],
    index: int,
    option_name: str,
) -> tuple[bool, str | None, int]:
    token = args[index]
    if token == option_name:
        if index + 1 >= len(args):
            raise AgyStartPreconditionError(f"{option_name} requires a value")
        return True, args[index + 1], 2
    if token.startswith(f"{option_name}="):
        return True, token[len(option_name) + 1 :], 1
    return False, None, 1


def project_agy_launch(
    spec: HarnessLaunchSpec,
    *,
    command: tuple[str, ...] = ("agy",),
    session_root: Path | None = None,
    worker_channel: WorkerChannel | None = None,
) -> AgyLaunchProjection:
    """Validate and translate HarnessLaunchSpec into ACP argv and session config."""

    filtered: list[str] = []
    arg_model: str | None = None
    arg_effort: str | None = None
    arg_mode: str | None = None
    arg_append_prompt: list[str] = []
    has_disable_telemetry = False
    skip_permissions = False

    index = 0
    raw_args = spec.args
    while index < len(raw_args):
        token = raw_args[index]
        matched, value, step = _split_option(raw_args, index, "--mode")
        if matched:
            assert value is not None
            if value == "auto":
                raise AgyStartPreconditionError(
                    "agy ACP does not support --mode auto; use --dangerously-skip-permissions "
                    "or one of: default, accept-edits, plan"
                )
            if value not in ACP_SUPPORTED_MODES:
                raise AgyStartPreconditionError(
                    f"unsupported agy ACP --mode {value!r}; expected default, accept-edits, or plan"
                )
            arg_mode = value
            index += step
            continue

        matched, value, step = _split_option(raw_args, index, "--effort")
        if matched:
            assert value is not None
            if not value.strip():
                raise AgyStartPreconditionError("--effort must not be empty")
            arg_effort = value
            index += step
            continue

        matched, value, step = _split_option(raw_args, index, "--model")
        if matched:
            assert value is not None
            if not value.strip():
                raise AgyStartPreconditionError("--model must not be empty")
            arg_model = value
            index += step
            continue

        matched, value, step = _split_option(raw_args, index, "--append-system-prompt")
        if matched:
            assert value is not None
            if value.strip():
                arg_append_prompt.append(value)
            index += step
            continue

        if token == "--disable-telemetry":
            has_disable_telemetry = True
        elif token == "--dangerously-skip-permissions":
            skip_permissions = True

        filtered.append(token)
        index += 1

    if spec.model is not None and arg_model is not None and spec.model != arg_model:
        raise AgyStartPreconditionError(
            "--model was supplied both as a hyprial option and after '--'"
        )
    if spec.effort is not None and arg_effort is not None and spec.effort != arg_effort:
        raise AgyStartPreconditionError(
            "--effort was supplied both as a hyprial option and after '--'"
        )

    model = spec.model if spec.model is not None else arg_model
    thought_level = spec.effort if spec.effort is not None else arg_effort

    prompt_parts: list[str] = []
    if worker_channel is not None:
        prompt_parts.append(_worker_system_prompt(worker_channel))
    prompt_parts.extend(arg_append_prompt)
    system_prompt_append = "\n\n".join(prompt_parts) if prompt_parts else None

    resolved = spec.resolved_command(command)
    argv: list[str] = [*resolved, "acp"]
    if session_root is not None:
        argv.extend(("--gemini-dir", str(session_root)))
    if not has_disable_telemetry:
        argv.append("--disable-telemetry")
    argv.extend(filtered)

    mcp_servers = (
        (worker_channel_to_acp_mcp_server(worker_channel),)
        if worker_channel is not None
        else ()
    )
    return AgyLaunchProjection(
        argv=tuple(argv),
        model=model,
        thought_level=thought_level,
        mode=arg_mode,
        system_prompt_append=system_prompt_append,
        dangerously_skip_permissions=skip_permissions,
        mcp_servers=mcp_servers,
        allowed_mcp_tools=allowed_harness_bridge_tool_names(worker_channel),
    )


def find_config_option(
    config_options: object,
    config_id: str,
) -> Mapping[str, object] | None:
    """Locate one entry in an ACP configOptions list by id or category."""

    if not isinstance(config_options, list):
        return None
    for entry in config_options:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("id") == config_id or entry.get("category") == config_id:
            return entry
    return None


def config_option_values(entry: Mapping[str, object] | None) -> set[str]:
    """Return all declared option values from an ACP configOption entry."""

    if entry is None:
        return set()
    options = entry.get("options")
    if not isinstance(options, list):
        return set()
    values: set[str] = set()
    for item in options:
        if isinstance(item, Mapping):
            val = item.get("value")
            if isinstance(val, str) and val:
                values.add(val)
    return values


def available_model_ids(session_result: Mapping[str, object]) -> set[str]:
    """Collect valid model identifiers from configOptions and models.availableModels."""

    ids = config_option_values(find_config_option(session_result.get("configOptions"), "model"))
    models = session_result.get("models")
    if isinstance(models, Mapping):
        available = models.get("availableModels")
        if isinstance(available, list):
            for item in available:
                if isinstance(item, Mapping):
                    model_id = item.get("modelId")
                    if isinstance(model_id, str) and model_id:
                        ids.add(model_id)
    return ids


def validate_agy_provider_environment(
    spec: HarnessLaunchSpec,
    environment: Mapping[str, str],
    *,
    allow_missing_google_key: bool = False,
) -> dict[str, str]:
    """Validate phase-1 model_provider selection for agy ACP."""

    if spec.model_provider is None:
        return {}
    if spec.model_provider != ACP_GOOGLE_PROVIDER:
        raise AgyStartPreconditionError(
            f"agy supports model provider 'google' or native OAuth, got {spec.model_provider!r}"
        )
    key = environment.get("GEMINI_API_KEY", "").strip()
    if not key and not allow_missing_google_key:
        raise AgyStartPreconditionError(
            "agy provider 'google' requires GEMINI_API_KEY"
        )
    return {"GEMINI_API_KEY": key} if key else {}


def validate_session_config_choice(
    *,
    config_id: str,
    value: str,
    session_result: Mapping[str, object],
    config_options: Sequence[Mapping[str, object]],
    model_name: str | None = None,
) -> None:
    """Validate a requested model, thought_level, or mode against server options."""

    if config_id == "model":
        valid = available_model_ids(session_result)
        if valid and value not in valid:
            raise AgyStartPreconditionError(f"unsupported agy model: {value!r}")
        return
    entry = find_config_option(list(config_options), config_id)
    valid_values = config_option_values(entry)
    if config_id == "thought_level":
        if entry is None or value not in valid_values:
            raise AgyStartPreconditionError(
                f"unsupported agy thought_level {value!r} for model {model_name or 'default'!r}"
            )
        return
    if valid_values and value not in valid_values:
        raise AgyStartPreconditionError(f"unsupported agy {config_id}: {value!r}")
