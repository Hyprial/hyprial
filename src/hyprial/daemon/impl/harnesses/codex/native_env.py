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
from dataclasses import replace
from collections.abc import Mapping
from collections.abc import Callable
from pathlib import Path

from hyprial.identity import (
    AgentRuntimeNotice,
    whitelist_replacement_environment,
)
from hyprial.kernel import PrivateAccessUnsupported, harden_private_path
from hyprial.identity import ConfigProjectionReceipt, verify_native_projection
from hyprial.identity import (
    AgentRuntimeContext,
    SharedCredentialBinding,
    validate_shared_credential_binding,
)

from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.codex.process import (
    CodexExecutableResolutionError,
    codex_launch_executables,
    resolve_codex_executable,
)
from hyprial.daemon.impl.harnesses.codex.launch.validation import (
    CodexNativeLoadEvidence as CodexNativeLoadEvidence,
)
from hyprial.daemon.impl.harnesses.codex.launch.validation import (
    _validate_codex_native_load as _validate_codex_native_load,
)
from hyprial.daemon.impl.harnesses.codex.projection import (
    CodexAgentHomeError,
    codex_projection_body_for_model_vendor,
    codex_projection_item_matches,
    private_directory as _private_directory,
    write_or_verify_projection_file as _write_or_verify_projection_file,
)

_CODEX_MUTABLE_ROOT_FILE = re.compile(
    r"(?:state|logs|goals|memories|queue|thread_history)_\d+\.sqlite(?:-shm|-wal)?\Z"
)

# Codex 0.157+ keeps an arg0 dispatch directory in CODEX_HOME across runs:
# one regular .lock file plus alias symlinks that resolve to the executable
# this daemon launches. Linux adds ``codex-linux-sandbox`` on top of the three
# aliases seen on macOS. Anything else under tmp/ stays refused.
_CODEX_ARG0_ENTRY = re.compile(r"tmp/arg0/codex-arg0[A-Za-z0-9]+/(?P<name>[^/]+)\Z")
_CODEX_ARG0_ALIASES = frozenset(
    {"apply_patch", "applypatch", "codex-execve-wrapper", "codex-linux-sandbox"}
)
_CODEX_MACOS_ARG0_LOCK = re.compile(
    r"tmp/arg0-macos/codex-arg0[A-Za-z0-9]{6}/\.lock\Z"
)
_CODEX_MUTABLE_FILES = frozenset(
    {
        ".sandbox_migration",
        # Codex SQLite housekeeping lock, not a personality/config input.
        ".sqlite-maintenance.lock",
        "app-server-control/app-server-startup.lock",
        "auth.json",
        "history.jsonl",
        "installation_id",
        # Codex refreshes this account/version-scoped model catalog after boot.
        # It is codex runtime state, not an agent-authored personality item.
        "models_cache.json",
        "version.json",
    }
)

_CODEX_MUTABLE_PREFIXES = (
    ".tmp/",
    "archived_sessions/",
    # Codex-written caches.  These carry no personality projection; the
    # projected config files keep their own byte/drift verification below.
    "cache/",
    "log/",
    "logs/",
    "plugins/",
    "shell_snapshots/",
    "skills/.system/",
    "thread-writer-locks/",
)

def _allowed_codex_mutable_file(relative: str) -> bool:
    return (
        relative in _CODEX_MUTABLE_FILES
        or _CODEX_MUTABLE_ROOT_FILE.fullmatch(relative) is not None
        or _CODEX_MACOS_ARG0_LOCK.fullmatch(relative) is not None
        or relative.startswith(_CODEX_MUTABLE_PREFIXES)
    )

def _verify_codex_native_inventory(
    native_root: Path,
    *,
    expected: frozenset[str],
    session_root: Path,
    codex_executable: Path | Callable[[], Path],
    shared_credential: SharedCredentialBinding | None = None,
) -> None:
    # Resolved lazily and once: only an arg0 alias needs the launched
    # executable, so a home without aliases never depends on codex's PATH.
    launched = functools.cache(
        (lambda: codex_executable) if isinstance(codex_executable, Path) else codex_executable
    )
    # The set of executables the launch chain may legitimately run: the
    # resolved launch target plus, for an npm shim, its vendored binary.
    acceptable = functools.cache(lambda: codex_launch_executables(launched()))
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
                if (
                    shared_credential is not None
                    and candidate == shared_credential.native_path
                ):
                    validate_shared_credential_binding(shared_credential)
                    continue
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
                    if target is not None and target in acceptable():
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
    model_provider: str | None = None,
    read_only: bool = False,
    shared_credential: SharedCredentialBinding | None = None,
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
        try:
            projected_body = source.read_bytes()
        except OSError as error:
            raise CodexAgentHomeError(
                f"cannot read Codex projection item {relative}"
            ) from error
        desired_body = codex_projection_body_for_model_vendor(
            relative, projected_body, model_provider
        )
        _write_or_verify_projection_file(
            source,
            destination,
            native_root=native_root,
            desired_body=desired_body,
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
        shared_credential=shared_credential,
    )

def _next_codex_auth_backup(context: AgentRuntimeContext) -> Path:
    root = context.roots.agent_home / "state" / "credential-backups" / "codex"
    current = context.roots.agent_home
    for part in Path("state/credential-backups/codex").parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise CodexAgentHomeError(
                "cannot create private Codex credential backup directory"
            ) from error
        try:
            harden_private_path(current, mode=0o700)
        except (OSError, PrivateAccessUnsupported) as error:
            raise CodexAgentHomeError(
                "cannot secure private Codex credential backup directory"
            ) from error
        _private_directory(current, "Codex credential backup directory")
    candidate = root / "auth.json"
    suffix = 0
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = root / f"auth.json.{suffix}"
    return candidate


def _backup_custom_provider_auth(
    context: AgentRuntimeContext,
) -> AgentRuntimeNotice | None:
    if context.model_provider in {None, "openai"}:
        return None
    auth = context.roots.native_root / "auth.json"
    try:
        metadata = auth.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CodexAgentHomeError("cannot inspect Codex auth.json") from error
    backup = _next_codex_auth_backup(context)
    if stat.S_ISREG(metadata.st_mode):
        try:
            harden_private_path(auth, mode=0o600)
            os.replace(auth, backup)
        except (OSError, PrivateAccessUnsupported) as error:
            raise CodexAgentHomeError(
                "cannot move custom-provider Codex auth.json to private backup"
            ) from error
    elif stat.S_ISLNK(metadata.st_mode):
        target = context.designated_shared_credential_target
        if target is None:
            raise CodexAgentHomeError(
                "custom-provider Codex auth.json link has no designated shared credential"
            )
        validate_shared_credential_binding(
            SharedCredentialBinding(
                actor=context.actor,
                harness="codex",
                native_path=auth,
                target_path=target,
                agent_cwd=None,
            )
        )
        try:
            body = auth.read_bytes()
            descriptor = os.open(
                backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            auth.unlink()
        except OSError as error:
            backup.unlink(missing_ok=True)
            raise CodexAgentHomeError(
                "cannot preserve linked Codex auth.json as a private backup"
            ) from error
    else:
        raise CodexAgentHomeError(
            "custom-provider Codex auth.json is not a regular file or credential link"
        )
    try:
        harden_private_path(backup, mode=0o600)
    except (OSError, PrivateAccessUnsupported) as error:
        raise CodexAgentHomeError(
            "Codex auth.json backup cannot be made account-private"
        ) from error
    message = (
        f"agent {context.actor} selected custom Codex provider "
        f"{context.model_provider}; moved inactive auth.json to {backup}"
    )
    return AgentRuntimeNotice(
        code="CODEX_CUSTOM_PROVIDER_AUTH_BACKUP",
        actor=context.actor,
        message=message,
        path=str(backup),
    )


def prepare_codex_runtime_context(
    context: AgentRuntimeContext,
    *,
    codex_executable: Path | Callable[[], Path] | None = None,
) -> AgentRuntimeContext:
    """Consume P22's authoritative roots without deriving a replacement set."""

    if context.harness != "codex":
        raise CodexAgentHomeError(
            f"Codex adapter received {context.harness!r} runtime context"
        )
    if context.environment().get("CODEX_HOME") != str(context.roots.native_root):
        raise CodexAgentHomeError(
            "Codex runtime context environment disagrees with its native root"
        )
    custom_provider = context.model_provider not in {None, "openai"}
    if custom_provider and context.shared_credential is not None:
        raise CodexAgentHomeError(
            "custom-provider Codex runtime cannot retain a shared auth.json binding"
        )
    if context.shared_credential is not None:
        validate_shared_credential_binding(context.shared_credential)
    notice = _backup_custom_provider_auth(context)
    if notice is not None:
        context = replace(context, notices=(*context.notices, notice))
    executable = codex_executable or (lambda: resolve_codex_executable(context.environment()))
    verify_native_projection(context.projection, context.roots.projection_root)
    prepare_codex_runtime_roots(
        projection_root=context.roots.projection_root,
        native_root=context.roots.native_root,
        session_root=context.roots.session_root,
        codex_executable=executable,
        receipt=context.projection_receipt,
        model_provider=context.model_provider,
        read_only=context.authority_prepared,
        shared_credential=context.shared_credential,
    )
    return context


def verify_codex_native_projection(
    receipt: ConfigProjectionReceipt,
    native_root: Path,
    *,
    model_provider: str | None = None,
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
        desired_body = codex_projection_body_for_model_vendor(
            item.native_path, projected_body, model_provider
        )
        body = candidate.read_bytes()
        if not codex_projection_item_matches(
            item.native_path, desired_body, body
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
    metadata = root / ".git"
    if not metadata.exists():
        if not root.is_dir():
            return []
        # A cwd below an existing worktree root is layout F: it already works
        # and must not gain a fictitious nested repository grant.  A genuine
        # non-repository cwd, however, needs permission for the `.git` path
        # before `git init`/clone-in-cwd can create it.
        try:
            existing_worktree = subprocess.run(
                ("git", "-C", str(root), "rev-parse", "--show-toplevel"),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            existing_worktree = False
        if existing_worktree:
            return []
        given = str(Path(os.path.abspath(metadata)))
        resolved = str(metadata.resolve(strict=False))
        return list(dict.fromkeys((given, resolved)))
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

    config: dict[str, object] = dict(execution.get("config", {}))
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
