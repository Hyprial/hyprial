# Decision record (harnesses/claude/runtime, W1E-1 2026-11-21):
# - Implements Owner 5A portability ruling to eliminate unprivileged Windows
#   os.symlink failures (SeCreateSymbolicLinkPrivilege / WinError 1314):
#   1. Receipt-named non-secret projection files are atomically copied as
#      private regular files into native_root and tracked in
#      .hyprial-projection.json so same-revision drift is rejected, authority-
#      prepared views stay read-only, and stale items from prior config
#      revisions are pruned without disturbing runtime-writable .claude.json or
#      .credentials.json.
#   2. P22 (identity/impl/agents/runtime/context.py), CLI session resume, and
#      transfer archiving (daemon/impl/transfer/archive/session_files.py)
#      require transcripts to live under the separate physical session_root
#      (<agent_home>/state/sessions/claude) outside secrets/native/claude.
#      Therefore <native_root>/projects uses manysys.link_directory /
#      manysys.is_directory_link / manysys.unlink_directory_link (POSIX directory
#      symlink, Windows unprivileged NTFS junction).
"""Claude-specific consumption of the shared agent-home P2 runtime context."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from hyprial.kernel import (
    PrivateAccessUnsupported,
    harden_private_path,
    manysys,
    private_metadata_violation,
)

from hyprial.identity import (
    AgentConfigError,
    NativeCredentialError,
    prepare_legacy_claude_native_root,
    verify_native_projection,
)
from hyprial.identity import (
    AgentRuntimeContext,
    validate_shared_credential_binding,
)

CLAUDE_RUNTIME_ENVIRONMENT = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
_SESSION_LINK = "projects"
_PROJECTION_STATE_FILE = ".hyprial-projection.json"
_LAUNCH_PROVISIONED_MARKER = ".hyprial-launch-provisioned"
_RESERVED_PROJECTION_ROOTS = frozenset(
    {_SESSION_LINK, ".credentials.json", _PROJECTION_STATE_FILE, _LAUNCH_PROVISIONED_MARKER}
)
_RUNTIME_WRITABLE_PATHS = frozenset({".claude.json"})
_DEAD_AUTH_NAME = "ANTHROPIC_OAUTH_TOKEN"
_OAUTH_AUTH_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
_EXPLICIT_AUTH_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    _OAUTH_AUTH_NAME,
)
CLAUDE_CREDENTIAL_MISSING = "CLAUDE_CREDENTIAL_MISSING"


class ClaudeRuntimeError(ValueError):
    """A resolved P2 context cannot be represented by Claude Code safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        permanent_start_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.permanent_start_failure = permanent_start_failure


def prepare_claude_runtime_context(context: AgentRuntimeContext) -> None:
    """Publish one verified Claude-native view without deriving any roots.

    P21 owns the immutable projection. P22 owns every resolved root. This
    adapter copies only the receipt-named non-secret projection files into the
    already-resolved native root, where Claude can load them alongside its
    separately authorized credential state. The ``projects`` entry is a
    harness-specific directory link to P22's session root.
    """

    try:
        _prepare_claude_runtime_context(context)
    except ClaudeRuntimeError:
        raise
    except AgentConfigError as error:
        raise ClaudeRuntimeError(str(error)) from error
    except OSError as error:
        raise ClaudeRuntimeError(
            f"cannot prepare Claude native runtime: {error}"
        ) from error


def _prepare_claude_runtime_context(context: AgentRuntimeContext) -> None:
    if context.harness != "claude":
        raise ClaudeRuntimeError("Claude runtime adapter requires harness 'claude'")
    if context.shared_credential is not None:
        validate_shared_credential_binding(context.shared_credential)
    roots = context.roots
    projection_root = roots.projection_root
    native_root = roots.native_root
    session_root = roots.session_root
    if context.projection_receipt.projection_root != str(projection_root):
        raise ClaudeRuntimeError("Claude projection receipt names another root")
    if context.projection_receipt.projection_digest != context.projection.digest:
        raise ClaudeRuntimeError("Claude projection receipt digest does not match")
    verify_native_projection(context.projection, projection_root)
    _require_private_directory(native_root, "native")
    _require_private_directory(session_root, "session")

    for item in context.projection.items:
        parts = PurePosixPath(item.native_path).parts
        if not parts or parts[0] in _RESERVED_PROJECTION_ROOTS:
            raise ClaudeRuntimeError(
                f"Claude projected path {item.native_path!r} is runtime-reserved"
            )

    _verify_or_plan_session_link(native_root / _SESSION_LINK, session_root)
    state = _read_projection_state(native_root)
    legacy_links = [
        entry
        for entry in native_root.iterdir()
        if entry.name not in _RESERVED_PROJECTION_ROOTS
        and _is_managed_projection_link(entry, context)
    ]

    if context.authority_prepared:
        if legacy_links or not _authority_projection_matches(context, state):
            raise ClaudeRuntimeError("authority-prepared Claude native projection drifted")
        if not manysys.is_directory_link(native_root / _SESSION_LINK):
            raise ClaudeRuntimeError("authority-prepared Claude projects link is missing")
        return

    if (
        state is not None
        and not legacy_links
        and state[0] == context.projection.digest
    ):
        _verify_copied_projection(context, state[1])
        _ensure_session_link(native_root / _SESSION_LINK, session_root)
        return

    previous_paths = state[1] if state is not None else frozenset()
    legacy_roots = {entry.name for entry in legacy_links}
    _check_projection_collisions(context, previous_paths, legacy_roots)

    for entry in legacy_links:
        entry.unlink()

    current_paths = frozenset(item.native_path for item in context.projection.items)
    for stale_rel in sorted(previous_paths - current_paths):
        _remove_projected_file(native_root, stale_rel)

    for item in context.projection.items:
        source = projection_root.joinpath(*PurePosixPath(item.native_path).parts)
        destination = native_root.joinpath(*PurePosixPath(item.native_path).parts)
        _copy_projection_item(native_root, source, destination)

    _write_projection_state(native_root, context.projection.digest, sorted(current_paths))
    _ensure_session_link(native_root / _SESSION_LINK, session_root)


def claude_runtime_session_file(
    context: AgentRuntimeContext, cwd: str, session_id: str
) -> Path:
    """Locate a Claude transcript directly under P22's authorized session root."""

    if context.harness != "claude":
        raise ClaudeRuntimeError("Claude session locator requires harness 'claude'")
    from hyprial.daemon.impl.transfer.archive.session_files import claude_project_dir_name

    projects = context.roots.session_root
    direct = projects / claude_project_dir_name(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    if not matches:
        from hyprial.daemon.impl.transfer.archive.session_files import SessionFileNotFound

        raise SessionFileNotFound(
            f"no claude session file for {session_id} under {projects}"
        )
    if len(matches) > 1:
        from hyprial.daemon.impl.transfer.archive.session_files import SessionFileAmbiguous

        raise SessionFileAmbiguous(
            f"claude session {session_id} found in multiple project dirs: "
            + ", ".join(str(match) for match in matches)
        )
    return matches[0]


def validate_claude_auth_environment(
    native_root: Path, environment: Mapping[str, str]
) -> None:
    """Reject ambiguous Claude auth without reading any credential value."""

    if _DEAD_AUTH_NAME in environment:
        raise ClaudeRuntimeError(
            f"Claude launch cannot use {_DEAD_AUTH_NAME}; use {_OAUTH_AUTH_NAME}"
        )
    selected = [name for name in _EXPLICIT_AUTH_NAMES if environment.get(name)]
    if len(selected) > 1:
        raise ClaudeRuntimeError(
            "Claude launch has conflicting explicit authentication methods"
        )
    credentials = Path(native_root) / ".credentials.json"
    if not credentials.exists() and not credentials.is_symlink():
        if selected:
            return
        raise _missing_claude_credential(credentials)
    try:
        metadata = credentials.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(
            f"cannot inspect Claude native credentials file: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ClaudeRuntimeError(
            "Claude native credentials path must be a regular file"
        )
    try:
        violation = private_metadata_violation(credentials, metadata, mode=0o600)
    except OSError as error:
        raise ClaudeRuntimeError(
            f"cannot inspect Claude native credentials privacy: {error}"
        ) from error
    if violation == "unsafe-mode":
        raise ClaudeRuntimeError("Claude native credentials file mode must be 0600")
    if violation is not None:
        raise ClaudeRuntimeError(
            f"Claude native credentials file must be account-private ({violation})"
        )
    if selected:
        raise ClaudeRuntimeError(
            "Claude native credentials and explicit authentication may not coexist"
        )
    if metadata.st_size == 0:
        raise _missing_claude_credential(credentials)


def validate_legacy_claude_auth(environment: dict[str, str]) -> str:
    """Do not inspect ambient native auth. Only the verified agent root is eligible."""
    home = environment.get("HYPRIAL_AGENT_DIR")
    selected = [name for name in _EXPLICIT_AUTH_NAMES if environment.get(name)]
    if _DEAD_AUTH_NAME in environment or len(selected) > 1:
        raise ClaudeRuntimeError("Claude launch has unsupported or conflicting authentication")
    if not home or not Path(home).is_absolute():
        raise _missing_claude_credential(Path("<agent-native>") / ".credentials.json")
    native_root = Path(home) / "secrets" / "native" / "claude"
    if not selected and environment.get("CLAUDE_CONFIG_DIR") != str(native_root):
        raise _missing_claude_credential(native_root / ".credentials.json")
    try:
        native_root, provisioned = prepare_legacy_claude_native_root(Path(home))
    except (NativeCredentialError, OSError) as error:
        raise ClaudeRuntimeError(str(error)) from error
    environment["CLAUDE_CONFIG_DIR"] = str(native_root)
    _prepare_legacy_session_root(Path(home), native_root)
    if not selected and provisioned:
        raise _missing_claude_credential(native_root / ".credentials.json")
    credentials = native_root / ".credentials.json"
    if not selected and (credentials.exists() or credentials.is_symlink()):
        validate_claude_auth_environment(native_root, environment)
    return "env-grant" if selected else "agent-native"


def _prepare_legacy_session_root(home: Path, native_root: Path) -> None:
    session_root = home / "state" / "sessions" / "claude"
    try:
        for root in (home / "state", home / "state/sessions", session_root):
            if not root.exists() and not root.is_symlink():
                root.mkdir(mode=0o700, exist_ok=True)
                _harden_private(root, mode=0o700, label="session root")
            _require_private_directory(root, "session")
        _ensure_session_link(native_root / _SESSION_LINK, session_root)
    except OSError as error:
        raise ClaudeRuntimeError(f"cannot prepare Claude session root: {error}") from error


def _missing_claude_credential(credentials: Path) -> ClaudeRuntimeError:
    methods = ", ".join(_EXPLICIT_AUTH_NAMES)
    return ClaudeRuntimeError(
        "Claude credential is missing: use agent provider login claude --actor <agent> "
        f"to install native credentials at {credentials}, or grant exactly one of "
        f"{methods} to the agent",
        code=CLAUDE_CREDENTIAL_MISSING,
        permanent_start_failure=True,
    )


def _harden_private(path: Path, *, mode: int, label: str) -> None:
    try:
        harden_private_path(path, mode=mode)
    except PrivateAccessUnsupported as error:
        raise ClaudeRuntimeError(f"Claude {label} cannot be made account-private: {error}") from error


def _require_private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(f"cannot inspect Claude {label} root: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ClaudeRuntimeError(f"Claude {label} root must be a real directory")
    try:
        violation = private_metadata_violation(path, metadata, mode=0o700)
    except OSError as error:
        raise ClaudeRuntimeError(
            f"cannot inspect Claude {label} root privacy: {error}"
        ) from error
    if violation == "unsafe-mode":
        raise ClaudeRuntimeError(f"Claude {label} root mode must be 0700")
    if violation is not None:
        raise ClaudeRuntimeError(
            f"Claude {label} root must be account-private ({violation})"
        )


def _read_projection_state(native_root: Path) -> tuple[str, frozenset[str]] | None:
    state_path = native_root / _PROJECTION_STATE_FILE
    try:
        metadata = state_path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or manysys.path_is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ClaudeRuntimeError("Claude projection state file is not a regular file")
    violation = private_metadata_violation(state_path, metadata, mode=0o600)
    if violation is not None:
        raise ClaudeRuntimeError(f"Claude projection state file must be private ({violation})")
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ClaudeRuntimeError("Claude projection state file is malformed") from error
    if not isinstance(payload, dict):
        raise ClaudeRuntimeError("Claude projection state file is malformed")
    digest = payload.get("projectionDigest")
    paths = payload.get("paths")
    if (
        not isinstance(digest, str)
        or not digest
        or not isinstance(paths, list)
        or any(not isinstance(item, str) or not item for item in paths)
    ):
        raise ClaudeRuntimeError("Claude projection state file is malformed")
    return digest, frozenset(paths)


def _write_projection_state(native_root: Path, digest: str, paths: list[str]) -> None:
    encoded = (
        json.dumps({"projectionDigest": digest, "paths": paths}, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write_private_file(native_root / _PROJECTION_STATE_FILE, encoded)


def _authority_projection_matches(
    context: AgentRuntimeContext, state: tuple[str, frozenset[str]] | None
) -> bool:
    expected_paths = frozenset(item.native_path for item in context.projection.items)
    if state is None or state[0] != context.projection.digest or state[1] != expected_paths:
        return False
    try:
        _verify_copied_projection(context, state[1])
    except ClaudeRuntimeError:
        return False
    return True


def _verify_copied_projection(
    context: AgentRuntimeContext, recorded_paths: frozenset[str]
) -> None:
    expected_paths = frozenset(item.native_path for item in context.projection.items)
    if recorded_paths != expected_paths:
        raise ClaudeRuntimeError("Claude native projection drifted")
    native_root = context.roots.native_root
    for item in context.projection.items:
        parts = PurePosixPath(item.native_path).parts
        current = native_root
        for directory_part in parts[:-1]:
            current = current / directory_part
            _require_private_directory(current, "native projection")
        candidate = native_root.joinpath(*parts)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError as error:
            raise ClaudeRuntimeError(
                f"Claude native projection drifted: {item.native_path}"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or manysys.path_is_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or private_metadata_violation(candidate, metadata, mode=0o600) is not None
        ):
            raise ClaudeRuntimeError(
                f"Claude native projection drifted: {item.native_path}"
            )
        if item.native_path in _RUNTIME_WRITABLE_PATHS:
            continue
        body = candidate.read_bytes()
        if len(body) != item.size or hashlib.sha256(body).hexdigest() != item.digest:
            raise ClaudeRuntimeError(
                f"Claude native projection drifted: {item.native_path}"
            )


def _check_projection_collisions(
    context: AgentRuntimeContext,
    previous_paths: frozenset[str],
    legacy_roots: set[str],
) -> None:
    native_root = context.roots.native_root
    previous_dirs = {
        PurePosixPath(*PurePosixPath(rel).parts[:depth]).as_posix()
        for rel in previous_paths
        for depth in range(1, len(PurePosixPath(rel).parts))
    }
    for item in context.projection.items:
        parts = PurePosixPath(item.native_path).parts
        if parts[0] in legacy_roots:
            continue
        for depth in range(1, len(parts)):
            prefix = PurePosixPath(*parts[:depth]).as_posix()
            parent = native_root.joinpath(*parts[:depth])
            try:
                meta = parent.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(meta.st_mode) or manysys.path_is_reparse(meta) or not stat.S_ISDIR(meta.st_mode):
                raise ClaudeRuntimeError(
                    f"Claude native path {parts[0]!r} collides with projected config"
                )
            if prefix not in previous_dirs and item.native_path not in previous_paths:
                raise ClaudeRuntimeError(
                    f"Claude native path {parts[0]!r} collides with projected config"
                )
        destination = native_root.joinpath(*parts)
        try:
            dest_meta = destination.lstat()
        except FileNotFoundError:
            continue
        if (
            item.native_path not in previous_paths
            or stat.S_ISLNK(dest_meta.st_mode)
            or manysys.path_is_reparse(dest_meta)
            or not stat.S_ISREG(dest_meta.st_mode)
        ):
            raise ClaudeRuntimeError(
                f"Claude native path {parts[0]!r} collides with projected config"
            )


def _copy_projection_item(native_root: Path, source: Path, destination: Path) -> None:
    relative_parent = destination.parent.relative_to(native_root)
    current = native_root
    for part in relative_parent.parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
            _harden_private(current, mode=0o700, label=f"projection directory {part}")
        _require_private_directory(current, "native projection")
    _atomic_write_private_file(destination, source.read_bytes())


def _atomic_write_private_file(destination: Path, body: bytes) -> None:
    temporary = destination.with_name(f".{destination.name}.hyprial-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    try:
        with open(temporary, "xb", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _harden_private(temporary, mode=0o600, label=f"projected file {destination.name}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_projected_file(native_root: Path, relative_path: str) -> None:
    parts = PurePosixPath(relative_path).parts
    target = native_root.joinpath(*parts)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode) and not manysys.path_is_reparse(metadata):
        target.unlink()
    parent = target.parent
    while parent != native_root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _is_managed_projection_link(path: Path, context: AgentRuntimeContext) -> bool:
    if not path.is_symlink():
        return False
    return _is_managed_projection_target(Path(os.readlink(path)), context)


def _is_managed_projection_target(
    target: Path, context: AgentRuntimeContext
) -> bool:
    if not target.is_absolute():
        return False
    config_root = context.roots.agent_home / "state" / "config"
    if not target.is_relative_to(config_root):
        return False
    return target.parent.name == "claude" and target.parent.parent.name == "native"


def _verify_or_plan_session_link(path: Path, session_root: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ClaudeRuntimeError(f"cannot inspect Claude session link: {error}") from error
    if not manysys.is_directory_link(path):
        raise ClaudeRuntimeError(
            "Claude native projects entry does not name the authorized session root"
        )
    try:
        resolved = path.resolve(strict=True)
        expected = session_root.resolve(strict=True)
    except OSError as error:
        raise ClaudeRuntimeError(
            "Claude native projects entry does not name the authorized session root"
        ) from error
    if resolved != expected or (
        stat.S_ISLNK(metadata.st_mode) and not Path(os.readlink(path)).is_absolute()
    ):
        raise ClaudeRuntimeError(
            "Claude native projects entry does not name the authorized session root"
        )


def _ensure_session_link(path: Path, session_root: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            manysys.link_directory(session_root, path)
        except FileExistsError:
            pass
    _verify_or_plan_session_link(path, session_root)
