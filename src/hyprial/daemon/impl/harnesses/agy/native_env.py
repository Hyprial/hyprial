# Decision record (file header; no inline comments by repository rule):
# - OAuth token projection uses atomic temp-file write + os.replace into
#   <session_root>/antigravity-cli/antigravity-oauth-token without symlinks or
#   chmod (win-compact W1E-1/W1E-2 and W012/W013 compliance).
# - Projection is strictly one-way from native_root to session_root; missing or
#   revoked native tokens actively remove the session_root copy.
"""Agent-home P2 runtime preparation for Antigravity (agy)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hyprial.identity import AgentRuntimeContext, validate_shared_credential_binding

AGY_OAUTH_TOKEN_FILENAME = "antigravity-oauth-token"
AGY_CLI_STATE_DIRNAME = "antigravity-cli"
_TMP_PREFIX = ".token.tmp."


class AgyAgentHomeError(ValueError):
    """Invalid or unsafe P2 agent-home state for agy."""


def _cleanup_stale_temp_files(target_dir: Path) -> None:
    if not target_dir.is_dir():
        return
    for entry in target_dir.iterdir():
        if not entry.name.startswith(_TMP_PREFIX):
            continue
        if entry.is_symlink():
            raise AgyAgentHomeError("agy token temp path must not be a symlink")
        if not entry.is_dir():
            entry.unlink(missing_ok=True)


def _validate_projection_paths(session_root: Path, target_dir: Path, dst: Path) -> None:
    if session_root.is_symlink() or not session_root.is_dir():
        raise AgyAgentHomeError("agy session root must be an existing directory")
    if target_dir.is_symlink():
        raise AgyAgentHomeError("agy cli state directory must not be a symlink")
    if target_dir.exists() and not target_dir.is_dir():
        raise AgyAgentHomeError("agy cli state path must be a directory")
    if dst.is_symlink():
        raise AgyAgentHomeError("agy oauth token target must not be a symlink")
    if dst.exists() and not dst.is_file():
        raise AgyAgentHomeError("agy oauth token target must be a regular file")


def sync_agy_oauth_token(*, native_root: Path, session_root: Path) -> str:
    """Atomically project or revoke antigravity-oauth-token in session_root."""

    src = native_root / AGY_OAUTH_TOKEN_FILENAME
    target_dir = session_root / AGY_CLI_STATE_DIRNAME
    dst = target_dir / AGY_OAUTH_TOKEN_FILENAME

    _validate_projection_paths(session_root, target_dir, dst)
    if src.is_symlink():
        raise AgyAgentHomeError("agy oauth token must be a regular file, not a symlink")

    if not src.exists():
        dst.unlink(missing_ok=True)
        _cleanup_stale_temp_files(target_dir)
        return "removed"

    if not src.is_file():
        raise AgyAgentHomeError("agy oauth token source must be a regular file")

    payload = src.read_bytes()
    if not payload:
        dst.unlink(missing_ok=True)
        _cleanup_stale_temp_files(target_dir)
        return "removed"

    if dst.is_file() and dst.read_bytes() == payload:
        return "unchanged"

    target_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_temp_files(target_dir)
    fd, tmp_name = tempfile.mkstemp(prefix=_TMP_PREFIX, dir=target_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return "copied"


def prepare_agy_runtime_context(context: AgentRuntimeContext) -> AgentRuntimeContext:
    """Prepare agy's isolated session_root without mutating native_root."""

    if context.harness != "agy":
        raise AgyAgentHomeError(
            f"agy runtime adapter requires harness 'agy', got {context.harness!r}"
        )
    if context.shared_credential is not None:
        validate_shared_credential_binding(context.shared_credential)
    sync_agy_oauth_token(
        native_root=context.roots.native_root,
        session_root=context.roots.session_root,
    )
    return context
