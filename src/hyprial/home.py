"""The explicit initialization boundary for the configured HYPRIAL home."""

from __future__ import annotations

import os
from collections.abc import Mapping
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
    """Return the implicit post-rename home.

    Legacy ``~/.h2b`` data is renamed by the explicit one-time migration in
    :mod:`hyprial.home_migration`; it is never selected as a runtime fallback.
    """

    return (Path.home() / ".hyprial").resolve(), "default value (~/.hyprial)"


_RETIRED_ENV_PREFIX = "H2B_"
_CURRENT_ENV_PREFIX = "HYPRIAL_"


class RetiredEnvironmentPrefix(RuntimeError):
    """A caller set a retired ``H2B_*`` variable that nothing reads any more.

    ⭐ Why this refuses instead of falling back: #440 renamed every environment
    variable this program reads, so ``H2B_HOME`` (and the rest of that family)
    became inert in one step.  A caller who sets only the retired name gets a
    process that runs happily against the **default** home -- i.e. the real one
    -- and reports success.  ⛔ The readers of those names are people: runbooks,
    cards, dispatch instructions and rehearsal scripts all still spell them the
    old way, and a person following one of those has no way to notice that the
    isolation they asked for was never applied.

    ⇒ So the failure is made loud at the boundary where the home is chosen, and
    the message names the replacement.  Setting *both* names is allowed: that is
    what a careful caller does while transitional material is still in the wild.
    """

    code = "HYPRIAL_RETIRED_ENV_PREFIX"

    def __init__(self, pairs: tuple[tuple[str, str], ...]) -> None:
        self.pairs = pairs
        self.data: dict[str, Any] = {
            "retired": [old for old, _ in pairs],
            "replacements": {old: new for old, new in pairs},
        }
        listed = ", ".join(f"{old} -> {new}" for old, new in pairs)
        super().__init__(
            f"retired environment variables are no longer read: {listed}"
        )


def reject_retired_environment(environ: Mapping[str, str] | None = None) -> None:
    """Refuse to continue when a retired name is set and its successor is not.

    ⛔ Anchored on the ``H2B_`` **prefix**, never on a substring: names such as
    ``TS_OAUTH_CLIENT_ID_H2B_CI`` are live third-party identifiers that merely
    contain those characters, and matching them would break CI.
    """

    env = os.environ if environ is None else environ
    pairs = tuple(
        (name, _CURRENT_ENV_PREFIX + name[len(_RETIRED_ENV_PREFIX) :])
        for name in sorted(env)
        if name.startswith(_RETIRED_ENV_PREFIX)
        and _CURRENT_ENV_PREFIX + name[len(_RETIRED_ENV_PREFIX) :] not in env
    )
    if pairs:
        raise RetiredEnvironmentPrefix(pairs)


def configured_hyprial_home(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    """Return the selected absolute home and the user-visible source label."""

    env = os.environ if environ is None else environ
    reject_retired_environment(env)
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


def initialize_hyprial_home() -> Path:
    """Create the selected home for init or login's shared onboarding path."""

    # A first ``hyprial init`` after installing the renamed distribution is
    # the install boundary we control.  Migrate before mkdir: creating an
    # empty destination first would turn a veteran install into an apparent
    # fresh machine and make the idempotence guard skip its data forever.
    from hyprial.home_migration import migrate_legacy_default_home

    migrate_legacy_default_home()
    path, _source = configured_hyprial_home()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        # ``mkdir(exist_ok=True)`` already rejects a regular file on normal
        # filesystems; keep the postcondition explicit for unusual backends.
        raise NotADirectoryError(f"HYPRIAL home is not a directory: {path}")
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
