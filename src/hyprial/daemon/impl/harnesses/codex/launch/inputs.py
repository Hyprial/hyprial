"""Typed Codex process launch inputs shared by attended and headless paths."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.harnesses.model_provider import (
    codex_provider_configuration,
)
from hyprial.kernel import HarnessLaunchSpec

HARNESS_BRIDGE_MCP_SERVER_NAME = "harness-bridge"


@dataclass(frozen=True, slots=True)
class CodexLaunchInputs:
    """Provider argv plus the exact app-server session inputs they accompany."""

    provider_argv: tuple[str, ...]
    argv: tuple[str, ...]
    expected_session_flags: dict[str, object]
    environment: dict[str, str]

    def app_server_command(
        self, command: tuple[str, ...], *server_args: str
    ) -> tuple[str, ...]:
        """Place every app-server config override after the subcommand."""

        return (*command, "app-server", *self.argv, *server_args)


def _provider_flags(argv: tuple[str, ...]) -> dict[str, object]:
    if not argv:
        return {}
    if len(argv) % 2 or argv[::2] != ("-c",) * (len(argv) // 2):
        raise ValueError("Codex provider configuration must contain -c pairs")
    try:
        return tomllib.loads("\n".join(argv[1::2]))
    except tomllib.TOMLDecodeError as error:
        raise ValueError("Codex provider configuration is invalid TOML") from error


def _toml_key(value: str) -> str:
    bare = value.replace("_", "a").replace("-", "a")
    return value if bare.isalnum() else json.dumps(value)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ",".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{_toml_key(str(key))}={_toml_value(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    raise TypeError(f"unsupported Codex launch input value {type(value).__name__}")


def _config_argv(flags: Mapping[str, object]) -> tuple[str, ...]:
    pairs: list[str] = []
    for key, value in flags.items():
        if key != "mcp_servers":
            pairs.append(f"{_toml_key(key)}={_toml_value(value)}")
            continue
        if not isinstance(value, Mapping):
            raise TypeError("Codex mcp_servers launch input must be a mapping")
        for server_name, raw_server in value.items():
            if not isinstance(raw_server, Mapping):
                raise TypeError("Codex MCP server launch input must be a mapping")
            prefix = f"mcp_servers.{_toml_key(str(server_name))}"
            for field in ("command", "args", "env"):
                pairs.append(f"{prefix}.{field}={_toml_value(raw_server[field])}")
    return tuple(part for pair in pairs for part in ("-c", pair))


def _interactive_server(
    *,
    actor: str,
    session_ref_file: Path,
    hyprial_home: Path,
    state_dir: Path,
) -> dict[str, object]:
    from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

    server = build_worker_channel(
        actor=actor,
        session_ref="pending",
        hyprial_home=hyprial_home,
        state_dir=state_dir,
    ).mcp_server
    raw_args = server.get("args")
    raw_env = server.get("env")
    command = server.get("command")
    if (
        not isinstance(command, str)
        or not isinstance(raw_args, (list, tuple))
        or not isinstance(raw_env, Mapping)
    ):
        raise ValueError("interactive Codex worker channel has invalid MCP config")
    args = [str(value) for value in raw_args]
    args.extend(("--session-ref-file", str(session_ref_file)))
    env = {
        str(key): str(value)
        for key, value in sorted(raw_env.items())
        if key != "HYPRIAL_WORKER_SESSION_REF"
    }
    return {"command": command, "args": args, "env": env}


def build_codex_launch_inputs(
    spec: HarnessLaunchSpec,
    environment: Mapping[str, str],
    *,
    tool_profile: bool = False,
    provider_session_flags: bool = False,
    interactive_channel: Mapping[str, object] | None = None,
    allow_legacy_home_fallback: bool = True,
) -> CodexLaunchInputs:
    """Build process ``-c`` args and the exact config/read layer they imply.

    Provider selection, the managed tool profile, and the attended
    harness-bridge mount all pass through this one function. Interactive
    app-server provider flags follow the subcommand so Codex reports them in
    sessionFlags. The headless CLI opts into the same provider session flags.
    The validator receives ``expected_session_flags`` verbatim; it never has
    to reconstruct launch intent from whichever keys Codex happened to show.
    """

    if interactive_channel is not None and spec.headless:
        raise ValueError("interactive Codex channel requires an attended launch")
    if interactive_channel is not None and provider_session_flags:
        raise ValueError(
            "interactive Codex provider session flags are selected automatically"
        )
    if provider_session_flags and not spec.headless:
        raise ValueError("Codex provider session flags require a headless launch")

    provider_argv, provider_environment = codex_provider_configuration(
        spec,
        environment,
        allow_legacy_home_fallback=allow_legacy_home_fallback,
    )
    # Parse once here so malformed provider output is refused at the shared
    # boundary. Interactive app-server overrides must follow the subcommand;
    # otherwise Codex does not apply them to config/read.
    provider_flags = _provider_flags(provider_argv)
    flags: dict[str, object] = (
        dict(provider_flags)
        if provider_session_flags or interactive_channel is not None
        else {}
    )
    if interactive_channel is not None and provider_flags:
        # A fresh agent home has no user config.toml. Custom providers do not
        # use Codex's file-backed OpenAI credential store.
        flags["cli_auth_credentials_store"] = "ephemeral"
    if interactive_channel is not None:
        required = {
            "actor",
            "session_ref_file",
            "hyprial_home",
            "state_dir",
        }
        if set(interactive_channel) != required:
            raise ValueError(
                "interactive Codex channel must contain actor, session_ref_file, "
                "hyprial_home, and state_dir"
            )
        flags["mcp_servers"] = {
            HARNESS_BRIDGE_MCP_SERVER_NAME: _interactive_server(
                actor=str(interactive_channel["actor"]),
                session_ref_file=Path(interactive_channel["session_ref_file"]),
                hyprial_home=Path(interactive_channel["hyprial_home"]),
                state_dir=Path(interactive_channel["state_dir"]),
            )
        }
    if tool_profile:
        flags.update(
            {
                "shell_environment_policy": {"inherit": "all"},
                "allow_login_shell": False,
            }
        )
    return CodexLaunchInputs(
        provider_argv=provider_argv,
        argv=_config_argv(flags),
        expected_session_flags=flags,
        environment=provider_environment,
    )
