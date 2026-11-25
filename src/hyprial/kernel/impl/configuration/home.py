# Decision record (W1E-2, 2026-11-21):
# - initialize_hyprial_home enforces the outer HYPRIAL_HOME privacy boundary as a
#   hard refusal via manysys.secure_private_directory (0o700 on POSIX, protected
#   inheritable current-user DACL on Windows), rejecting symlinks, reparse points,
#   non-directories, wrong owners, and unsafe ancestry before writing state.
# - configured_hyprial_home, default_hyprial_home, and configured_harness_state_dir
#   inspect the lexical abspath before calling .resolve(): any symlink or Windows
#   reparse point on the target leaf or on a non-system ancestor (outside POSIX
#   top-level /tmp, /var, /etc) preserves the unresolved lexical path so downstream
#   hard gates reject the link instead of silently following it, while normal paths
#   on macOS and POSIX still resolve canonical system prefixes.
"""The explicit initialization boundary for the configured HYPRIAL home."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import hyprial.kernel.impl.manysys.interface as manysys
from hyprial.kernel.impl.manysys.interface import (
    PrivateAccessUnsupported,
    secure_private_directory,
)


def _is_allowed_system_symlink(step: Path, target: Path) -> bool:
    return (
        step != target
        and step.anchor == "/"
        and step.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
    )


def _has_unsafe_link_or_reparse(target: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(target)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if manysys.path_is_reparse(observed):
            return True
        if stat.S_ISLNK(observed.st_mode):
            if not _is_allowed_system_symlink(current, lexical) or not current.is_dir():
                return True
    return False


def _normalize_configured_path(raw: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(raw)))
    if _has_unsafe_link_or_reparse(lexical):
        return lexical
    return lexical.resolve()


class HYPRIALHomeNotInitialized(RuntimeError):
    """A command was pointed at a home that ``hyprial init`` has not created."""

    code = "HYPRIAL_HOME_NOT_INITIALIZED"

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.data: dict[str, Any] = {"path": str(path), "source": source}
        super().__init__(
            f"HYPRIAL home does not exist or is not a directory: {path} "
            f"(source: {source})"
        )


def default_hyprial_home() -> tuple[Path, str]:
    """Return the implicit home."""

    return (
        _normalize_configured_path(Path.home() / ".hyprial"),
        "default value (~/.hyprial)",
    )


def configured_hyprial_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    """Return the selected absolute home and the user-visible source label."""

    env = os.environ if environ is None else environ
    configured = env.get("HYPRIAL_HOME")
    if configured is not None:
        return (
            _normalize_configured_path(Path(configured).expanduser()),
            "HYPRIAL_HOME environment variable",
        )
    return default_hyprial_home()


def require_initialized_hyprial_home() -> Path:
    """Reject a missing or linked configured home without creating directories."""

    path, source = configured_hyprial_home()
    if _has_unsafe_link_or_reparse(path):
        raise PrivateAccessUnsupported("unsafe-directory")
    try:
        observed = path.lstat()
    except FileNotFoundError:
        raise HYPRIALHomeNotInitialized(path, source) from None
    if stat.S_ISLNK(observed.st_mode) or manysys.path_is_reparse(observed):
        raise PrivateAccessUnsupported("unsafe-directory")
    if not stat.S_ISDIR(observed.st_mode):
        raise HYPRIALHomeNotInitialized(path, source)
    return path


def configured_harness_state_dir(
    environ: Mapping[str, str] | None = None,
    *,
    home_selector: Callable[[], Path] | None = None,
) -> Path:
    """Select the state root without initializing it or selecting home eagerly.

    An explicit nonempty state path outranks home, including sibling roots.
    """
    env = os.environ if environ is None else environ
    configured = env.get("HARNESS_STATE_DIR")
    if configured:
        return _normalize_configured_path(Path(configured).expanduser())
    home = home_selector() if home_selector is not None else configured_hyprial_home(env)[0]
    return home / "state"


def initialize_hyprial_home(
    *,
    ensure_dispatch_policy: Callable[[Path], object],
) -> Path:
    """Create the selected home around required upper-layer initialization effects.

    The caller owns dispatch policy; the callback is required so an
    unassembled init cannot silently omit it.

    A node id is minted only into a home that is new (absent or empty).
    Re-running init on an existing home must not change its node id: stored
    addresses carry it, and an existing install writes its old value itself.
    """

    from hyprial.kernel.impl.primitives.node_identity import mint_node_id

    path, _source = configured_hyprial_home()
    if _has_unsafe_link_or_reparse(path):
        raise PrivateAccessUnsupported("unsafe-directory")
    try:
        observed = path.lstat()
    except FileNotFoundError:
        observed = None
    if observed is not None:
        if stat.S_ISLNK(observed.st_mode) or manysys.path_is_reparse(observed):
            raise PrivateAccessUnsupported("unsafe-directory")
        if not stat.S_ISDIR(observed.st_mode):
            raise NotADirectoryError(f"HYPRIAL home is not a directory: {path}")
    new_home = observed is None or not any(path.iterdir())
    secure_private_directory(path, create=True, mode=0o700)
    if new_home:
        mint_node_id(path)
    ensure_dispatch_policy(path)
    return path


def child_state_environment(hyprial_home: Path, state_dir: Path) -> dict[str, str]:
    """Environment that pins a child process to this daemon's home.

    ``HYPRIAL_HOME`` always; ``HARNESS_STATE_DIR`` only when the state root
    really lives outside ``<home>/state`` (a service install may put it
    there).  A child that inherits only ``HYPRIAL_HOME`` can re-point itself
    with one explicit ``HYPRIAL_HOME`` -- the isolation boundary a test rig
    expects -- whereas an inherited ``HARNESS_STATE_DIR`` used to outrank
    that and silently aim the rig at this daemon (card 85dd41e2: 77
    fixture actors reached the production registry that way).
    """
    home = Path(hyprial_home).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve()
    environment = {"HYPRIAL_HOME": str(home)}
    if state != home / "state":
        environment["HARNESS_STATE_DIR"] = str(state)
    return environment
