"""Codex agent-home projection, native-load validation, thread config
and turn-result parsing helpers."""
from __future__ import annotations


import functools
import hashlib
import os
import re
import stat
import subprocess
import sys as sys
from collections.abc import Mapping
from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path

from hyprial.identity import (
    whitelist_replacement_environment,
)
from hyprial.identity import ConfigProjectionReceipt, verify_native_projection
from hyprial.identity import (
    AgentRuntimeContext,
    SharedCredentialBinding,
    validate_shared_credential_binding,
)

from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.codex.process import (
    CodexExecutableResolutionError,
    resolve_codex_executable,
)
from hyprial.daemon.impl.harnesses.codex.projection import (
    CodexAgentHomeError,
    codex_projection_item_matches,
    private_directory as _private_directory,
    write_or_verify_projection_file as _write_or_verify_projection_file,
)

_CODEX_SUPPORTED_AUTH_STORES = frozenset({"file", "ephemeral"})

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

_CODEX_MUTABLE_ROOT_FILE = re.compile(
    r"(?:state|logs|goals|memories|queue|thread_history)_\d+\.sqlite(?:-shm|-wal)?\Z"
)

# Codex 0.157+ keeps an arg0 dispatch directory in CODEX_HOME across runs:
# one regular .lock file plus three exact alias symlinks that resolve to the
# executable this daemon launches. Anything else under tmp/ stays refused.
_CODEX_ARG0_ENTRY = re.compile(r"tmp/arg0/codex-arg0[A-Za-z0-9]+/(?P<name>[^/]+)\Z")
_CODEX_ARG0_ALIASES = frozenset(
    {"apply_patch", "applypatch", "codex-execve-wrapper"}
)

_CODEX_MUTABLE_FILES = frozenset(
    {
        ".sandbox_migration",
        "auth.json",
        "history.jsonl",
        "installation_id",
        "version.json",
    }
)

_CODEX_MUTABLE_PREFIXES = (
    ".tmp/",
    "archived_sessions/",
    "log/",
    "logs/",
    "shell_snapshots/",
    "skills/.system/",
    "thread-writer-locks/",
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

def _allowed_codex_mutable_file(relative: str) -> bool:
    return (
        relative in _CODEX_MUTABLE_FILES
        or _CODEX_MUTABLE_ROOT_FILE.fullmatch(relative) is not None
        or relative.startswith(_CODEX_MUTABLE_PREFIXES)
    )

def _verify_codex_native_inventory(
    native_root: Path,
    *,
    expected: frozenset[str],
    session_root: Path,
    codex_executable: Path | Callable[[], Path],
) -> None:
    # Resolved lazily and once: only an arg0 alias needs the launched
    # executable, so a home without aliases never depends on codex's PATH.
    launched = functools.cache(
        (lambda: codex_executable) if isinstance(codex_executable, Path) else codex_executable
    )
    stack = [native_root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise CodexAgentHomeError(
                f"cannot scan Codex native root at {directory.name}"
            ) from error
        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(native_root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise CodexAgentHomeError(
                    f"cannot inspect Codex native item {relative}"
                ) from error
            arg0 = _CODEX_ARG0_ENTRY.fullmatch(relative)
            if stat.S_ISLNK(metadata.st_mode):
                if arg0 is not None and arg0.group("name") in _CODEX_ARG0_ALIASES:
                    try:
                        link = Path(os.readlink(candidate))
                    except OSError as error:
                        raise CodexAgentHomeError(f"Codex arg0 alias {relative} cannot be read") from error
                    try:
                        target = link.resolve(strict=True) if link.is_absolute() else None
                    except (OSError, RuntimeError):
                        target = None
                    try:
                        expected_target = launched()
                    except CodexExecutableResolutionError as error:
                        raise CodexAgentHomeError(
                            f"Codex arg0 alias {relative} -> {link} is refused: {error}"
                        ) from error
                    if target == expected_target:
                        continue
                    raise CodexAgentHomeError(
                        f"Codex arg0 alias {relative} -> {link} is an unauthorized "
                        f"symbolic link; it must be an absolute link to {expected_target}"
                    )
                if relative != "sessions" or candidate.resolve() != session_root.resolve():
                    raise CodexAgentHomeError(
                        f"Codex native item {relative} is an unauthorized symbolic link"
                    )
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if _CODEX_MUTABLE_ROOT_FILE.fullmatch(relative):
                    raise CodexAgentHomeError(
                        f"Codex native database {relative} is not a regular file"
                    )
                stack.append(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CodexAgentHomeError(
                    f"Codex native item {relative} is not a regular file"
                )
            if arg0 is not None and arg0.group("name") == ".lock":
                continue
            if relative not in expected and not _allowed_codex_mutable_file(relative):
                raise CodexAgentHomeError(
                    f"Codex native root contains unprojected personality item {relative}"
                )

def prepare_codex_runtime_roots(
    *,
    projection_root: Path,
    native_root: Path,
    session_root: Path,
    codex_executable: Path | Callable[[], Path],
    receipt: ConfigProjectionReceipt | None = None,
    read_only: bool = False,
) -> None:
    """Publish an already-resolved P21 projection into the mutable Codex root."""

    projection_root = Path(projection_root)
    native_root = Path(native_root)
    session_root = Path(session_root)
    for path, label in (
        (projection_root, "Codex projection root"),
        (native_root, "resolved CODEX_HOME"),
        (session_root, "Codex session root"),
    ):
        _private_directory(path, label)
    if receipt is not None:
        if Path(receipt.projection_root) != projection_root:
            raise CodexAgentHomeError(
                "Codex projection receipt does not name the resolved projection root"
            )
        if receipt.harness != "codex":
            raise CodexAgentHomeError(
                f"native projection belongs to {receipt.harness!r}, not 'codex'"
            )
        expected = frozenset(item.native_path for item in receipt.items)
    else:
        expected_items: list[str] = []
        for candidate in projection_root.rglob("*"):
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CodexAgentHomeError("Codex projection contains a symbolic link")
            if stat.S_ISREG(metadata.st_mode):
                expected_items.append(candidate.relative_to(projection_root).as_posix())
        expected = frozenset(expected_items)
    for relative in sorted(expected):
        source = projection_root.joinpath(*Path(relative).parts)
        destination = native_root.joinpath(*Path(relative).parts)
        _write_or_verify_projection_file(
            source,
            destination,
            native_root=native_root,
            read_only=read_only,
        )
    sessions = native_root / "sessions"
    if sessions.exists() or sessions.is_symlink():
        if not sessions.is_symlink() or sessions.resolve() != session_root.resolve():
            raise CodexAgentHomeError(
                "Codex sessions path is not bound to the resolved session root"
            )
    elif read_only:
        raise CodexAgentHomeError("authority-prepared Codex sessions link is missing")
    else:
        sessions.symlink_to(session_root, target_is_directory=True)
    _verify_codex_native_inventory(
        native_root,
        expected=expected,
        session_root=session_root,
        codex_executable=codex_executable,
    )

def prepare_codex_runtime_context(
    context: AgentRuntimeContext,
    *,
    codex_executable: Path | Callable[[], Path] | None = None,
) -> None:
    """Consume P22's authoritative roots without deriving a replacement set."""

    if context.harness != "codex":
        raise CodexAgentHomeError(
            f"Codex adapter received {context.harness!r} runtime context"
        )
    if context.environment().get("CODEX_HOME") != str(context.roots.native_root):
        raise CodexAgentHomeError(
            "Codex runtime context environment disagrees with its native root"
        )
    if context.shared_credential is not None:
        validate_shared_credential_binding(context.shared_credential)
    executable = codex_executable or (lambda: resolve_codex_executable(context.environment()))
    verify_native_projection(context.projection, context.roots.projection_root)
    prepare_codex_runtime_roots(
        projection_root=context.roots.projection_root,
        native_root=context.roots.native_root,
        session_root=context.roots.session_root,
        codex_executable=executable,
        receipt=context.projection_receipt,
        read_only=context.authority_prepared,
    )

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

def _validate_codex_native_load(
    *,
    initialize: object,
    config_read: object,
    account_read: object,
    skills_list: object,
    native_root: Path,
    cwd: Path,
    model_provider: str | None,
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
    if stat.S_IMODE(root_metadata.st_mode) != 0o700:
        raise CodexAgentHomeError("resolved CODEX_HOME mode must be 0700")
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
            project_mcp = layer_config.get("mcp_servers")
            if project_mcp is not None:
                project_mcp_config = _require_mapping(
                    project_mcp, "project config mcp_servers"
                )
                if HARNESS_BRIDGE_MCP_SERVER_NAME in project_mcp_config:
                    raise CodexAgentHomeError(
                        "Codex project config attempted to override reserved MCP "
                        f"server {HARNESS_BRIDGE_MCP_SERVER_NAME}"
                    )
        if layer_type == "user":
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
    if session_flags is not None:
        provider_session_flags = {
            "model",
            "model_provider",
            "model_providers",
            "model_reasoning_effort",
        }
        tool_session_flags = {"allow_login_shell", "shell_environment_policy"}
        allowed_session_flags = provider_session_flags | tool_session_flags
        extras = sorted(set(session_flags) - allowed_session_flags)
        if extras:
            raise CodexAgentHomeError(
                "Codex session-flags layer is not an approved launch input: "
                f"{extras[0]}"
            )
        observed_provider_flags = provider_session_flags.intersection(session_flags)
        if bool(observed_provider_flags) != custom_provider:
            raise CodexAgentHomeError(
                "Codex session-flags provider overrides do not match the launch mode"
            )
        if custom_provider and session_flags.get("model_provider") != model_provider:
            raise CodexAgentHomeError(
                "Codex session-flags provider does not match the launch selection"
            )
        if custom_provider:
            providers = _require_mapping(
                session_flags.get("model_providers"),
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
        observed_tool_flags = tool_session_flags.intersection(session_flags)
        if bool(observed_tool_flags) != require_tool_profile:
            raise CodexAgentHomeError(
                "Codex session-flags tool profile does not match the launch mode"
            )
        if require_tool_profile and (
            session_flags.get("allow_login_shell") is not False
            or session_flags.get("shell_environment_policy") != {"inherit": "all"}
        ):
            raise CodexAgentHomeError(
                "Codex session-flags tool profile does not preserve the approved environment"
            )
    elif custom_provider or require_tool_profile:
        raise CodexAgentHomeError(
            "Codex managed launch omitted its session-flags layer"
        )
    expected_user_config = expected_root / "config.toml"
    if len(user_files) != 1 or user_files[0].resolve() != expected_user_config.resolve():
        raise CodexAgentHomeError(
            "Codex user config layer did not come from resolved CODEX_HOME/config.toml"
        )
    try:
        config_metadata = expected_user_config.lstat()
    except OSError as error:
        raise CodexAgentHomeError("Codex user config.toml is unreadable") from error
    if stat.S_ISLNK(config_metadata.st_mode) or not stat.S_ISREG(config_metadata.st_mode):
        raise CodexAgentHomeError("Codex user config.toml must be a regular file")
    if stat.S_IMODE(config_metadata.st_mode) != 0o600:
        raise CodexAgentHomeError("Codex user config.toml mode must be 0600")

    auth_store = config.get("cli_auth_credentials_store")
    if auth_store not in _CODEX_SUPPORTED_AUTH_STORES:
        raise CodexAgentHomeError(
            f"Codex credential store {auth_store!r} is unsupported for agent-home P2"
        )
    auth_path = expected_root / "auth.json"
    auth_present = auth_path.exists() or auth_path.is_symlink()
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
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise CodexAgentHomeError("Codex auth.json mode must be 0600")
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
    if custom_provider and auth_present:
        raise CodexAgentHomeError(
            "Codex found both native auth.json and custom-provider authentication"
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

def verify_codex_native_projection(
    receipt: ConfigProjectionReceipt, native_root: Path
) -> None:
    """Verify P21's Codex projection bytes at the P22-resolved mutable root."""

    if receipt.harness != "codex":
        raise CodexAgentHomeError(
            f"native projection belongs to {receipt.harness!r}, not 'codex'"
        )
    root = Path(native_root)
    try:
        metadata = root.lstat()
    except OSError as error:
        raise CodexAgentHomeError(f"cannot inspect resolved CODEX_HOME: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CodexAgentHomeError("resolved CODEX_HOME must be a real directory")
    for item in receipt.items:
        candidate = root.joinpath(*Path(item.native_path).parts)
        projected = Path(receipt.projection_root).joinpath(
            *Path(item.native_path).parts
        )
        try:
            item_metadata = candidate.lstat()
        except OSError as error:
            raise CodexAgentHomeError(
                f"Codex native projection item {item.native_path} is missing"
            ) from error
        if stat.S_ISLNK(item_metadata.st_mode) or not stat.S_ISREG(item_metadata.st_mode):
            raise CodexAgentHomeError(
                f"Codex native projection item {item.native_path} is not a regular file"
            )
        try:
            projected_body = projected.read_bytes()
        except OSError as error:
            raise CodexAgentHomeError(
                f"Codex projection source item {item.native_path} is missing"
            ) from error
        if (
            len(projected_body) != item.size
            or hashlib.sha256(projected_body).hexdigest() != item.digest
        ):
            raise CodexAgentHomeError(
                f"Codex projection source item {item.native_path} drifted"
            )
        body = candidate.read_bytes()
        if not codex_projection_item_matches(
            item.native_path, projected_body, body
        ):
            raise CodexAgentHomeError(
                f"Codex native projection item {item.native_path} drifted"
            )

#: The thread-scoped MCP server name under which the worker channel is
#: registered.  It is also the ONLY server whose tool approvals the unattended
#: client grants (card 87dc8276): the tools behind it are hyprial's own.
HARNESS_BRIDGE_MCP_SERVER_NAME = "harness-bridge"

def _worker_channel_config(channel: WorkerChannel) -> dict[str, object]:
    """The channel's MCP server as a thread-scoped Codex config override.

    Codex app-server applies thread/start (and thread/resume) ``config``
    entries through its normal override stack, and per-thread MCP servers
    listed there are assembled for that thread (verified live against
    codex-cli 0.147.0: the injected server reports
    mcpServer/startupStatus/updated = ready, bound to the thread id).
    Codex infers the stdio transport from ``command`` and has no ``type``
    field, so the Claude-SDK-shaped key is dropped here.
    """

    server = {key: value for key, value in channel.mcp_server.items() if key != "type"}
    # `default_tools_approval_mode = "auto"` on this server: accepted and kept
    # by codex (2026-08-31 arm B, `codex mcp get`), and MEASURED INERT on the
    # app-server path 2026-09-05 (codex-cli 0.152.0, gpt-5.6-sol, E2E-006
    # codex case, rollouts kept): under `-a never` the model's harness_reply
    # call inside codex's exec runtime is refused ("MCP tool call requires
    # approval, but approval policy is never") with this key present; under
    # `-a on-request` the native reply succeeds with no prompt, with or
    # without this key.  The approval policy decides; this key does not
    # beat a global `never` and is not needed otherwise.  It stays because
    # the wire tests pin it by name and codex drops unknown keys silently
    # (2026-08-31 arm C), so its presence is at least verifiable.
    server["default_tools_approval_mode"] = "auto"
    # ``approvals_reviewer = "user"`` routes this thread's approvals to the
    # client (hyprial) instead of codex's auto-reviewer.  Measured 2026-09-06 (CI
    # #4971 rollouts): under ``-a on-request`` the global ``auto_review``
    # reviewer runs on the worker's model provider and, on DeepSeek, fails --
    # every harness_* call was rejected and the model replied nothing.  With
    # the reviewer set per thread, ChatGPT and DeepSeek workers behave alike
    # and the approval lands in ``_server_request_response`` below.
    return {
        "mcp_servers": {HARNESS_BRIDGE_MCP_SERVER_NAME: server},
        "approvals_reviewer": "user",
    }

def _git_metadata_roots(cwd: str | Path) -> list[str]:
    """The git metadata paths a commit in ``cwd`` needs to write.

    Codex's ``workspace-write`` sandbox keeps the ``.git`` directory at the
    top of a writable root READ-ONLY (measured 2026-09-14, codex-cli 0.153.4,
    macOS seatbelt; spec ``notes/spec-codex-worker-git-commit-2026-09-14.md``
    table A-E): a repository whose root is the sandbox cwd cannot create
    ``.git/index.lock`` (``Operation not permitted``), while the same
    repository nested one level down is fine -- and so is a repository whose
    ``.git`` is outside the writable root.  The fix is to add the
    repository's own git metadata to
    ``sandbox_workspace_write.writable_roots``.

    Only the worktree ROOT is affected; a cwd below the root already works
    (layout F, measured), so this returns ``[]`` unless ``.git`` sits
    directly at ``cwd``.  A plain clone's ``.git`` is a directory; a linked
    worktree's is a file whose real metadata lives at
    ``git rev-parse --git-dir`` (index/HEAD) and ``--git-common-dir``
    (objects/refs).  Both must be writable, so both are returned; a linked
    worktree needs the common dir for object writes even when its per-tree
    git dir is already writable.

    Paths are read with an absolute-format ``git rev-parse`` so the answer
    does not depend on the daemon's cwd.  A missing/unusable ``git`` falls
    back to ``<cwd>/.git`` rather than silently granting nothing.
    """

    root = Path(cwd)
    if not (root / ".git").exists():
        return []
    candidates: list[str] = []
    for arguments in (
        ("--absolute-git-dir",),
        ("--path-format=absolute", "--git-common-dir"),
    ):
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), "rev-parse", *arguments),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            candidates.append(value)
    if not candidates:
        candidates.append(str((root / ".git").resolve()))
    roots: list[str] = []
    for candidate in candidates:
        if candidate not in roots:
            roots.append(candidate)
    return roots

def _sandbox_writable_roots_config(
    cwd: str | Path, execution: Mapping[str, object]
) -> dict[str, object] | None:
    """Thread-config fragment granting the repo's git metadata write access.

    Only meaningful under ``workspace-write``.  An explicitly non-workspace
    mode (``read-only`` or ``danger-full-access``) gets nothing -- the first
    is unwritable by design and the second needs no grant.  When the CLI did
    not name a sandbox the effective mode comes from the operator's codex
    config (this host sets ``sandbox_mode = "workspace-write"``), so the
    grant is added there too: under ``read-only`` it is inert, and omitting
    it would leave exactly the config-defaulted path broken.
    """

    sandbox = execution.get("sandbox")
    if sandbox is not None and sandbox != "workspace-write":
        return None
    roots = _git_metadata_roots(cwd)
    if not roots:
        return None
    return {"sandbox_workspace_write": {"writable_roots": roots}}

def _thread_config(
    cwd: str | Path,
    execution: Mapping[str, object],
    worker_channel: WorkerChannel | None,
    *,
    managed_environment: bool = False,
) -> dict[str, object]:
    """Assemble the ``thread/start`` / ``thread/resume`` ``config`` block.

    Both overrides ride the ONE channel hyprial already uses for thread
    config (``mcp_servers`` + ``approvals_reviewer``), so there is no second
    configuration path to keep in sync.  An empty mapping is dropped by the
    caller so threads without either stay byte-identical on the wire.
    """

    config: dict[str, object] = {}
    if worker_channel is not None:
        config.update(_worker_channel_config(worker_channel))
    if managed_environment:
        # The app-server already runs under P22's complete replacement env.
        # Inheriting that exact set gives tool subprocesses the approved tool
        # profile without serializing any credential value into thread config.
        # Login shells stay disabled so shell rc files cannot add a second,
        # mutable personality source after the environment was approved.
        config.update(
            {
                "shell_environment_policy": {"inherit": "all"},
                "allow_login_shell": False,
            }
        )
    sandbox_roots = _sandbox_writable_roots_config(cwd, execution)
    if sandbox_roots is not None:
        config.update(sandbox_roots)
    return config

def _server_request_response(
    identifier: object, method: str, params: object = None
) -> dict[str, object]:
    """Fail closed for unattended approvals while keeping the turn alive.

    One exception, deliberately narrow: an MCP tool-call approval for the
    worker's own harness-bridge server is granted.  Measured 2026-09-06
    (codex-cli 0.152.0, ``approvals_reviewer = "user"``): the request arrives
    as ``mcpServer/elicitation/request`` with ``serverName`` and
    ``_meta.codex_approval_kind == "mcp_tool_call"``; answering
    ``{"action": "accept", "content": {}}`` runs the tool, anything else --
    including the -32601 this client used to send -- is recorded by codex as
    "user rejected MCP tool call".  Every other elicitation is declined.

    Every server request that the codex 0.153.4 v2 protocol can send is
    answered with a SCHEMA-VALID fail-closed result (Schemas generated with
    ``codex app-server generate-json-schema`` on 2026-09-14):

    * approvals -> ``decline`` / an empty permission grant;
    * ``item/tool/requestUserInput`` -> ``{"answers": {}}`` (no user is
      attached; codex accepts the empty map and the model continues);
    * ``item/tool/call`` -> an unsuccessful result, so a dynamic tool the
      client does not implement fails the CALL instead of the transport.

    The -32601 fallback is deliberately kept only for methods this client
    has never seen; the read loop logs it (``codex.server_request.unsupported``)
    so a newly introduced request name is visible rather than silently
    treated as answered.
    """

    if method == "mcpServer/elicitation/request":
        request = params if isinstance(params, dict) else {}
        meta = request.get("_meta")
        kind = meta.get("codex_approval_kind") if isinstance(meta, dict) else None
        if (
            kind == "mcp_tool_call"
            and request.get("serverName") == HARNESS_BRIDGE_MCP_SERVER_NAME
        ):
            return {"id": identifier, "result": {"action": "accept", "content": {}}}
        return {"id": identifier, "result": {"action": "decline"}}
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"id": identifier, "result": {"decision": "decline"}}
    if method == "item/permissions/requestApproval":
        return {
            "id": identifier,
            "result": {"permissions": {}, "scope": "turn"},
        }
    if method == "item/tool/requestUserInput":
        # No human is attached to an unattended worker.  The response shape
        # is required (``answers`` is not nullable), and an empty map is the
        # honest "no answer available"; measured 2026-09-14, codex completes
        # the turn on it instead of leaving the blocking question open.
        return {"id": identifier, "result": {"answers": {}}}
    if method == "item/tool/call":
        return {
            "id": identifier,
            "result": {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": (
                            "unattended hyprial worker has no client-side "
                            "dynamic tool implementation"
                        ),
                    }
                ],
                "success": False,
            },
        }
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {
            "id": identifier,
            "result": {
                "decision": {
                    "denied": {
                        "rejection": "unattended hyprial worker cannot grant approval"
                    }
                }
            },
        }
    return {
        "id": identifier,
        "error": {
            "code": -32601,
            "message": f"hyprial Codex app-server client does not support server request {method!r}",
        },
    }

def _turn_error(turn: dict[str, object], turn_id: str) -> str:
    error = turn.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return f"Codex turn {turn_id!r} ended with status {turn.get('status')!r}"

def _final_reply(turn: dict[str, object]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if (
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return None


class _CodexSpawnEnvironmentMixin:
        def _spawn_environment(self) -> dict[str, str] | None:
            """The exact env mapping the exec consumer receives (B2).
    
            Complete-replacement mode returns the frozen mapping plus this
            client's own delta (the resolved provider key and the worker
            identity), the same ``{**base, **delta}`` form the connector and the
            Agent SDK carrier use: argv names ``env_key="DEEPSEEK_API_KEY"``, so
            dropping the delta leaves codex failing every turn on a missing
            variable.  Legacy callers keep the whitelist-filtered form.
            Wholesale ``os.environ`` merging is gone from this carrier either
            way.
            """
    
            if self._complete_launch is not None:
                return {
                    **self._complete_launch.environment.for_exec(),
                    **(self._env or {}),
                }
            return None if self._env is None else whitelist_replacement_environment(
                os.environ, self._env
            )
