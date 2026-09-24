"""Claude-specific consumption of the shared agent-home P2 runtime context."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from hyprial.agents.config import AgentConfigError, verify_native_projection
from hyprial.agents.runtime import AgentRuntimeContext

CLAUDE_RUNTIME_ENVIRONMENT = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
_SESSION_LINK = "projects"
_RESERVED_PROJECTION_ROOTS = frozenset({_SESSION_LINK, ".credentials.json"})
_EXPLICIT_AUTH_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_OAUTH_TOKEN",
)


class ClaudeRuntimeError(ValueError):
    """A resolved P2 context cannot be represented by Claude Code safely."""


def prepare_claude_runtime_context(context: AgentRuntimeContext) -> None:
    """Publish one verified Claude-native view without deriving any roots.

    P21 owns the immutable projection. P22 owns every resolved root. This
    adapter copies only the receipt-named non-secret projection files into the
    already-resolved native root, where Claude can load them alongside its
    separately authorized credential state. The ``projects`` entry is a
    harness-specific indirection to P22's session root.
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

    projected_roots: dict[str, Path] = {}
    for item in context.projection.items:
        parts = PurePosixPath(item.native_path).parts
        if not parts or parts[0] in _RESERVED_PROJECTION_ROOTS:
            raise ClaudeRuntimeError(
                f"Claude projected path {item.native_path!r} is runtime-reserved"
            )
        projected_roots[parts[0]] = projection_root / parts[0]

    planned = [
        _plan_projection_link(
            native_root / name,
            source,
            context,
        )
        for name, source in sorted(projected_roots.items())
    ]
    _verify_or_plan_session_link(native_root / _SESSION_LINK, session_root)
    stale = [
        entry
        for entry in native_root.iterdir()
        if entry.name not in projected_roots
        and entry.name not in _RESERVED_PROJECTION_ROOTS
        and _is_managed_projection_link(entry, context)
    ]
    for path, source, replace in planned:
        if replace:
            _replace_projection_link(path, source)
    for path in stale:
        path.unlink()
    _ensure_session_link(native_root / _SESSION_LINK, session_root)


def claude_runtime_session_file(
    context: AgentRuntimeContext, cwd: str, session_id: str
) -> Path:
    """Locate a Claude transcript directly under P22's authorized session root."""

    if context.harness != "claude":
        raise ClaudeRuntimeError("Claude session locator requires harness 'claude'")
    from hyprial.transfer.session_files import claude_project_dir_name

    projects = context.roots.session_root
    direct = projects / claude_project_dir_name(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"))
    if not matches:
        from hyprial.transfer.session_files import SessionFileNotFound

        raise SessionFileNotFound(
            f"no claude session file for {session_id} under {projects}"
        )
    if len(matches) > 1:
        from hyprial.transfer.session_files import SessionFileAmbiguous

        raise SessionFileAmbiguous(
            f"claude session {session_id} found in multiple project dirs: "
            + ", ".join(str(match) for match in matches)
        )
    return matches[0]


def validate_claude_auth_environment(
    native_root: Path, environment: Mapping[str, str]
) -> None:
    """Reject ambiguous Claude auth without reading any credential value."""

    selected = [name for name in _EXPLICIT_AUTH_NAMES if environment.get(name)]
    if len(selected) > 1:
        raise ClaudeRuntimeError(
            "Claude launch has conflicting explicit authentication methods"
        )
    credentials = Path(native_root) / ".credentials.json"
    if not credentials.exists() and not credentials.is_symlink():
        return
    try:
        metadata = credentials.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(
            f"cannot inspect Claude native credentials file: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ClaudeRuntimeError(
            "Claude native credentials path must be a regular file"
        )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ClaudeRuntimeError("Claude native credentials file mode must be 0600")
    if selected:
        raise ClaudeRuntimeError(
            "Claude native credentials and explicit authentication may not coexist"
        )


def _require_private_directory(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(f"cannot inspect Claude {label} root: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ClaudeRuntimeError(f"Claude {label} root must be a real directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ClaudeRuntimeError(f"Claude {label} root mode must be 0700")


def _plan_projection_link(
    path: Path, source: Path, context: AgentRuntimeContext
) -> tuple[Path, Path, bool]:
    if not path.exists() and not path.is_symlink():
        return path, source, True
    if not path.is_symlink():
        raise ClaudeRuntimeError(
            f"Claude native path {path.name!r} collides with projected config"
        )
    target = Path(os.readlink(path))
    if target == source:
        return path, source, False
    if not _is_managed_projection_target(target, context):
        raise ClaudeRuntimeError(
            f"Claude native path {path.name!r} is not a managed projection link"
        )
    return path, source, True


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


def _replace_projection_link(path: Path, source: Path) -> None:
    temporary = path.with_name(f".{path.name}.hyprial-{os.getpid()}")
    try:
        os.symlink(source, temporary, target_is_directory=source.is_dir())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _verify_or_plan_session_link(path: Path, session_root: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ClaudeRuntimeError(f"cannot inspect Claude session link: {error}") from error
    if not stat.S_ISLNK(metadata.st_mode) or os.readlink(path) != str(session_root):
        raise ClaudeRuntimeError(
            "Claude native projects entry does not name the authorized session root"
        )


def _ensure_session_link(path: Path, session_root: Path) -> None:
    if path.exists() or path.is_symlink():
        _verify_or_plan_session_link(path, session_root)
        return
    try:
        os.symlink(session_root, path, target_is_directory=True)
    except FileExistsError:
        _verify_or_plan_session_link(path, session_root)
