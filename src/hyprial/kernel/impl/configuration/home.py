"""The explicit initialization boundary for the configured HYPRIAL home."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class HYPRIALHomeNotInitialized(RuntimeError):
    """A command was pointed at a home that ``hyprial init`` has not created."""

    code = "HYPRIAL_HOME_NOT_INITIALIZED"

    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.data: dict[str, Any] = {"path": str(path), "source": source}
        # Fact-reporting contract (contract/cli-blackbox): name the path and
        # where it came from; remedies live in skills/docs, not error strings.
        super().__init__(
            f"HYPRIAL home does not exist or is not a directory: {path} "
            f"(source: {source})"
        )


def default_hyprial_home() -> tuple[Path, str]:
    """Return the implicit home."""

    return (Path.home() / ".hyprial").resolve(), "default value (~/.hyprial)"


def configured_hyprial_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    """Return the selected absolute home and the user-visible source label."""

    env = os.environ if environ is None else environ
    configured = env.get("HYPRIAL_HOME")
    if configured is not None:
        return (
            Path(configured).expanduser().resolve(),
            "HYPRIAL_HOME environment variable",
        )
    return default_hyprial_home()


def require_initialized_hyprial_home() -> Path:
    """Reject a missing configured home without creating any directories."""

    path, source = configured_hyprial_home()
    if not path.is_dir():
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
        return Path(configured).expanduser().resolve()
    home = home_selector() if home_selector is not None else configured_hyprial_home(env)[0]
    return home / "state"


def initialize_hyprial_home(
    *,
    ensure_dispatch_policy: Callable[[Path], object],
) -> Path:
    """Create the selected home around required upper-layer initialization effects.

    The caller owns dispatch policy; the callback is required so an
    unassembled init cannot silently omit it.
    """

    path, _source = configured_hyprial_home()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        # ``mkdir(exist_ok=True)`` already rejects a regular file on normal
        # filesystems; keep the postcondition explicit for unusual backends.
        raise NotADirectoryError(f"HYPRIAL home is not a directory: {path}")
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
