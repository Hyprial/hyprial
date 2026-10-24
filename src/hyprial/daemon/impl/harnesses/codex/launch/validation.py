"""Validation of Codex native-load receipts against exact launch inputs."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hyprial.identity import (
    SharedCredentialBinding,
    validate_shared_credential_binding,
)
from hyprial.kernel import private_metadata_violation

from hyprial.daemon.impl.harnesses.codex.projection import CodexAgentHomeError

_CODEX_SUPPORTED_AUTH_STORES = frozenset({"file", "ephemeral"})
# This allowlist is deliberately independent of launch-input construction.
# A builder mutation must still be refused even when it also mutates the
# expected receipt handed to this validator.
_CODEX_APPROVED_SESSION_FLAGS = frozenset(
    {
        "allow_login_shell",
        "cli_auth_credentials_store",
        "mcp_servers",
        "model",
        "model_provider",
        "model_providers",
        "model_reasoning_effort",
        "shell_environment_policy",
    }
)
_CODEX_APPROVED_MCP_SERVERS = frozenset({"harness-bridge"})
_CODEX_APPROVED_MCP_FIELDS = frozenset({"command", "args", "env"})
_HARNESS_BRIDGE_MCP_SERVER_NAME = "harness-bridge"
_CODEX_PROJECT_RESERVED_KEYS = frozenset(
    {
        "allow_login_shell",
        "approvals_reviewer",
        "chatgpt_base_url",
        "cli_auth_credentials_store",
        "forced_login_method",
        "model_provider",
        "model_providers",
        "openai_base_url",
        "shell_environment_policy",
    }
)


@dataclass(frozen=True, slots=True)
class CodexNativeLoadEvidence:
    """Non-secret observations returned by the real Codex app-server."""

    codex_home: str
    layer_types: tuple[str, ...]
    effective_model: str | None
    auth_store: str
    account_type: str | None
    requires_openai_auth: bool
    user_skills: tuple[str, ...]
    project_skills: tuple[str, ...]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _nearest_project_root(cwd: Path) -> Path:
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CodexAgentHomeError(f"Codex app-server {label} must be an object")
    return value


def _require_private(
    path: Path, metadata: os.stat_result, *, mode: int, label: str
) -> None:
    try:
        violation = private_metadata_violation(path, metadata, mode=mode)
    except OSError as error:
        raise CodexAgentHomeError(f"{label} privacy cannot be inspected") from error
    if violation == "unsafe-mode":
        raise CodexAgentHomeError(f"{label} mode must be {mode:04o}")
    if violation is not None:
        raise CodexAgentHomeError(f"{label} must be account-private ({violation})")


def _validate_codex_native_load(
    *,
    initialize: object,
    config_read: object,
    account_read: object,
    skills_list: object,
    native_root: Path,
    cwd: Path,
    model_provider: str | None,
    expected_session_flags: Mapping[str, object] | None = None,
    require_tool_profile: bool = False,
    shared_credential: SharedCredentialBinding | None = None,
) -> CodexNativeLoadEvidence:
    """Validate real app-server receipts before a P2 thread starts or resumes."""

    expected_root = Path(native_root)
    if not expected_root.is_absolute():
        raise CodexAgentHomeError("resolved CODEX_HOME must be absolute")
    try:
        root_metadata = expected_root.lstat()
    except OSError as error:
        raise CodexAgentHomeError("resolved CODEX_HOME is unreadable") from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise CodexAgentHomeError("resolved CODEX_HOME must be a real directory")
    _require_private(
        expected_root, root_metadata, mode=0o700, label="resolved CODEX_HOME"
    )
    initialized = _require_mapping(initialize, "initialize result")
    observed_home = initialized.get("codexHome")
    if not isinstance(observed_home, str) or not observed_home:
        raise CodexAgentHomeError("Codex initialize omitted codexHome")
    if Path(observed_home).resolve() != expected_root.resolve():
        raise CodexAgentHomeError(
            "Codex initialize codexHome does not match the resolved CODEX_HOME"
        )

    config_result = _require_mapping(config_read, "config/read result")
    config = _require_mapping(config_result.get("config"), "effective config")
    raw_layers = config_result.get("layers")
    if not isinstance(raw_layers, list):
        raise CodexAgentHomeError("Codex config/read omitted layers")
    project_root = _nearest_project_root(Path(cwd))
    layer_types: list[str] = []
    user_files: list[Path] = []
    user_layer_configs: list[dict[str, object]] = []
    system_files: list[Path] = []
    session_flags: dict[str, object] | None = None
    for index, raw_layer in enumerate(raw_layers):
        layer = _require_mapping(raw_layer, f"config layer {index}")
        name = _require_mapping(layer.get("name"), f"config layer {index} name")
        layer_type = name.get("type")
        if layer_type not in {"sessionFlags", "project", "user", "system"}:
            raise CodexAgentHomeError(
                f"Codex config/read returned unsupported layer {layer_type!r}"
            )
        layer_types.append(layer_type)
        layer_config = _require_mapping(
            layer.get("config"), f"config layer {index} config"
        )
        if layer_type == "sessionFlags":
            if session_flags is not None:
                raise CodexAgentHomeError(
                    "Codex config/read returned multiple session-flags layers"
                )
            session_flags = layer_config
        if layer_type == "project":
            raw_folder = name.get("dotCodexFolder")
            if (
                not isinstance(raw_folder, str)
                or Path(raw_folder).name != ".codex"
                or not _inside(Path(raw_folder), project_root)
            ):
                raise CodexAgentHomeError(
                    "Codex project config escaped the repository root"
                )
            reserved = sorted(_CODEX_PROJECT_RESERVED_KEYS.intersection(layer_config))
            if reserved:
                raise CodexAgentHomeError(
                    "Codex project config attempted to override reserved key "
                    f"{reserved[0]}"
                )
        if layer_type != "sessionFlags":
            layer_mcp = layer_config.get("mcp_servers")
            if layer_mcp is not None:
                layer_mcp_config = _require_mapping(
                    layer_mcp, f"{layer_type} config mcp_servers"
                )
                if _HARNESS_BRIDGE_MCP_SERVER_NAME in layer_mcp_config:
                    raise CodexAgentHomeError(
                        f"Codex {layer_type} config attempted to override reserved MCP "
                        f"server {_HARNESS_BRIDGE_MCP_SERVER_NAME}"
                    )
        if layer_type == "user":
            user_layer_configs.append(layer_config)
            raw_file = name.get("file")
            if isinstance(raw_file, str) and raw_file:
                user_files.append(Path(raw_file))
        if layer_type == "system":
            raw_file = name.get("file")
            if isinstance(raw_file, str) and raw_file:
                system_files.append(Path(raw_file))
    if tuple(layer_types) != tuple(
        sorted(
            layer_types,
            key={"sessionFlags": 0, "project": 1, "user": 2, "system": 3}.get,
        )
    ):
        raise CodexAgentHomeError(
            "Codex config layer order must be session flags, project, user, system"
        )
    if layer_types.count("user") != 1 or layer_types.count("system") != 1:
        raise CodexAgentHomeError(
            "Codex config/read must contain exactly one user and system layer"
        )
    if system_files != [Path("/etc/codex/config.toml")]:
        raise CodexAgentHomeError(
            "Codex system config layer must remain /etc/codex/config.toml"
        )
    custom_provider = model_provider not in {None, "openai"}
    legacy_session_contract = expected_session_flags is None
    expected_flags = dict(expected_session_flags or {})
    observed_flags = {} if session_flags is None else session_flags
    if legacy_session_contract:
        provider_keys = {
            "model",
            "model_provider",
            "model_providers",
            "model_reasoning_effort",
        }
        observed_provider_keys = provider_keys.intersection(observed_flags)
        if bool(observed_provider_keys) != custom_provider:
            raise CodexAgentHomeError(
                "Codex session-flags provider overrides do not match the launch mode"
            )
        if custom_provider and observed_flags.get("model_provider") != model_provider:
            raise CodexAgentHomeError(
                "Codex session-flags provider does not match the launch selection"
            )
        if custom_provider:
            providers = _require_mapping(
                observed_flags.get("model_providers"),
                "session-flags model_providers",
            )
            provider_config = _require_mapping(
                providers.get(str(model_provider)),
                "session-flags selected provider",
            )
            if provider_config.get("requires_openai_auth") is not False:
                raise CodexAgentHomeError(
                    "Codex custom provider session flags must disable OpenAI authentication"
                )
            env_key = provider_config.get("env_key")
            if not isinstance(env_key, str) or not env_key:
                raise CodexAgentHomeError(
                    "Codex custom provider session flags must name an environment key"
                )
        tool_keys = {"allow_login_shell", "shell_environment_policy"}
        observed_tool_keys = tool_keys.intersection(observed_flags)
        if bool(observed_tool_keys) != require_tool_profile:
            raise CodexAgentHomeError(
                "Codex session-flags tool profile does not match the launch mode"
            )
        if require_tool_profile and (
            observed_flags.get("allow_login_shell") is not False
            or observed_flags.get("shell_environment_policy") != {"inherit": "all"}
        ):
            raise CodexAgentHomeError(
                "Codex session-flags tool profile does not preserve the approved environment"
            )
        if "mcp_servers" in observed_flags:
            raise CodexAgentHomeError(
                "Codex session-flags layer is not an approved launch input: mcp_servers"
            )
        expected_flags = dict(observed_flags)
    elif expected_flags and session_flags is None:
        raise CodexAgentHomeError(
            "Codex managed launch omitted its session-flags layer"
        )
    for flags in (observed_flags, expected_flags):
        extras = sorted(set(flags) - _CODEX_APPROVED_SESSION_FLAGS)
        if extras:
            raise CodexAgentHomeError(
                "Codex session-flags layer is not an approved launch input: "
                f"{extras[0]}"
            )
    observed_mcp = observed_flags.get("mcp_servers")
    expected_mcp = expected_flags.get("mcp_servers")
    if observed_mcp is not None or expected_mcp is not None:
        for label, raw_servers in (
            ("session-flags", observed_mcp),
            ("expected session-flags", expected_mcp),
        ):
            servers = _require_mapping(raw_servers, f"{label} mcp_servers")
            extra_servers = sorted(set(servers) - _CODEX_APPROVED_MCP_SERVERS)
            if extra_servers:
                raise CodexAgentHomeError(
                    "Codex session-flags layer is not an approved launch input: "
                    f"mcp_servers.{extra_servers[0]}"
                )
            if set(servers) != _CODEX_APPROVED_MCP_SERVERS:
                raise CodexAgentHomeError(
                    "Codex session-flags mcp_servers must contain only harness-bridge"
                )
            bridge = _require_mapping(
                servers.get(_HARNESS_BRIDGE_MCP_SERVER_NAME),
                f"{label} harness-bridge MCP server",
            )
            bridge_extras = sorted(set(bridge) - _CODEX_APPROVED_MCP_FIELDS)
            if bridge_extras:
                raise CodexAgentHomeError(
                    "Codex session-flags layer is not an approved launch input: "
                    "mcp_servers.harness-bridge."
                    f"{bridge_extras[0]}"
                )
            if set(bridge) != _CODEX_APPROVED_MCP_FIELDS:
                raise CodexAgentHomeError(
                    "Codex session-flags harness-bridge must contain command, args, and env"
                )
    if observed_flags != expected_flags:
        tool_keys = {"allow_login_shell", "shell_environment_policy"}
        if any(observed_flags.get(key) != expected_flags.get(key) for key in tool_keys):
            raise CodexAgentHomeError(
                "Codex session-flags tool profile does not preserve the approved environment"
            )
        raise CodexAgentHomeError(
            "Codex session-flags layer does not match the approved launch inputs"
        )

    if not legacy_session_contract:
        effective_provider = config.get("model_provider")
        provider_matches = (
            effective_provider == model_provider
            if custom_provider
            else effective_provider in {None, "openai"}
        )
        if not provider_matches:
            raise CodexAgentHomeError(
                "Codex effective model provider does not match the launch selection"
            )
        if custom_provider:
            providers = _require_mapping(
                config.get("model_providers"), "effective model_providers"
            )
            provider_config = _require_mapping(
                providers.get(str(model_provider)), "effective selected provider"
            )
            if provider_config.get("requires_openai_auth") is not False:
                raise CodexAgentHomeError(
                    "Codex custom provider must disable OpenAI authentication"
                )
            env_key = provider_config.get("env_key")
            if not isinstance(env_key, str) or not env_key:
                raise CodexAgentHomeError(
                    "Codex custom provider must name an environment key"
                )
    expected_user_config = expected_root / "config.toml"
    if (
        len(user_files) != 1
        or user_files[0].resolve() != expected_user_config.resolve()
    ):
        raise CodexAgentHomeError(
            "Codex user config layer did not come from resolved CODEX_HOME/config.toml"
        )
    try:
        config_metadata = expected_user_config.lstat()
    except FileNotFoundError:
        config_metadata = None
    except OSError as error:
        raise CodexAgentHomeError("Codex user config.toml is unreadable") from error
    if config_metadata is None:
        if user_layer_configs != [{}]:
            raise CodexAgentHomeError(
                "Codex missing user config.toml produced a non-empty user layer"
            )
    else:
        if stat.S_ISLNK(config_metadata.st_mode) or not stat.S_ISREG(
            config_metadata.st_mode
        ):
            raise CodexAgentHomeError("Codex user config.toml must be a regular file")
        _require_private(
            expected_user_config,
            config_metadata,
            mode=0o600,
            label="Codex user config.toml",
        )

    auth_store = config.get("cli_auth_credentials_store")
    if auth_store not in _CODEX_SUPPORTED_AUTH_STORES:
        raise CodexAgentHomeError(
            f"Codex credential store {auth_store!r} is unsupported for agent-home P2"
        )
    auth_path = expected_root / "auth.json"
    auth_present = auth_path.exists() or auth_path.is_symlink()
    if custom_provider and auth_present:
        raise CodexAgentHomeError(
            "Codex found both native auth.json and custom-provider authentication"
        )
    if auth_store == "file":
        if shared_credential is not None:
            if shared_credential.native_path != auth_path:
                raise CodexAgentHomeError(
                    "Codex shared credential binding names another native path"
                )
            validate_shared_credential_binding(shared_credential)
        else:
            try:
                metadata = auth_path.lstat()
            except OSError as error:
                raise CodexAgentHomeError(
                    "Codex file credential store requires agent-owned auth.json"
                ) from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise CodexAgentHomeError("Codex auth.json must be a regular file")
            _require_private(auth_path, metadata, mode=0o600, label="Codex auth.json")
    elif auth_present:
        raise CodexAgentHomeError(
            "Codex ephemeral credential store must not fall back to auth.json"
        )

    account_result = _require_mapping(account_read, "account/read result")
    requires_openai_auth = account_result.get("requiresOpenaiAuth")
    if not isinstance(requires_openai_auth, bool):
        raise CodexAgentHomeError("Codex account/read omitted requiresOpenaiAuth")
    raw_account = account_result.get("account")
    account_type: str | None = None
    if raw_account is not None:
        account = _require_mapping(raw_account, "account/read account")
        raw_type = account.get("type")
        if isinstance(raw_type, str) and raw_type:
            account_type = raw_type
    if custom_provider and requires_openai_auth:
        raise CodexAgentHomeError(
            "Codex custom provider still requires OpenAI authentication"
        )
    if requires_openai_auth and account_type is None:
        raise CodexAgentHomeError(
            "Codex account/read found no authenticated subject for this process"
        )

    skills_result = _require_mapping(skills_list, "skills/list result")
    rows = skills_result.get("data")
    if not isinstance(rows, list):
        raise CodexAgentHomeError("Codex skills/list omitted data")
    row = next(
        (
            item
            for item in rows
            if isinstance(item, dict) and item.get("cwd") == str(cwd)
        ),
        None,
    )
    if row is None:
        raise CodexAgentHomeError("Codex skills/list omitted the requested cwd")
    errors = row.get("errors")
    if errors not in (None, []):
        raise CodexAgentHomeError("Codex skills/list reported loader errors")
    raw_skills = row.get("skills")
    if not isinstance(raw_skills, list):
        raise CodexAgentHomeError("Codex skills/list omitted skills")
    user_skills: list[str] = []
    project_skills: list[str] = []
    for raw_skill in raw_skills:
        skill = _require_mapping(raw_skill, "skill entry")
        name = skill.get("name")
        path = skill.get("path")
        scope = skill.get("scope")
        if not isinstance(name, str) or not isinstance(path, str):
            raise CodexAgentHomeError("Codex skill entry omitted name or path")
        skill_path = Path(path)
        if scope == "user":
            if not _inside(skill_path, expected_root / "skills"):
                raise CodexAgentHomeError(
                    f"Codex user skill {name!r} escaped resolved CODEX_HOME"
                )
            user_skills.append(name)
        elif scope == "repo":
            if not _inside(skill_path, project_root):
                raise CodexAgentHomeError(
                    f"Codex project skill {name!r} escaped the repository root"
                )
            project_skills.append(name)
        elif scope == "system":
            if not _inside(skill_path, expected_root / "skills" / ".system"):
                raise CodexAgentHomeError(
                    f"Codex system skill {name!r} escaped resolved CODEX_HOME"
                )
        else:
            raise CodexAgentHomeError(
                f"Codex skill {name!r} used unsupported scope {scope!r}"
            )

    return CodexNativeLoadEvidence(
        codex_home=str(expected_root),
        layer_types=tuple(layer_types),
        effective_model=(
            config.get("model") if isinstance(config.get("model"), str) else None
        ),
        auth_store=str(auth_store),
        account_type=account_type,
        requires_openai_auth=requires_openai_auth,
        user_skills=tuple(sorted(user_skills)),
        project_skills=tuple(sorted(project_skills)),
    )
