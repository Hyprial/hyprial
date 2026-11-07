"""Identity generation: the CLI -> daemon launch check (no lock handoff).

A login commits identity under the home identity-transaction lock and records
``identityGeneration`` (monotonic per credential lineage) in the same atomic
``secrets/login.json`` write as the credential.  It then *releases* the lock
and starts a daemon with ``HYPRIAL_EXPECTED_IDENTITY_GENERATION``.  The daemon
acquires the lock itself before reading identity and compares: a later login
that slipped into the gap has a newer generation, so this daemon exits with
:data:`IDENTITY_GENERATION_STALE_EXIT_CODE` and the earlier login is reported
``superseded`` (last writer wins, observably).

T11 exception (recorded, owner-visible in CHANGELOG): the daemon now opens
``secrets/login.json``, but only to read this integer -- never the refresh
token -- so its identity source is still ``HYPRIAL_OWNER > settings.owner``.

Monotonic across logout: logout deletes ``login.json``, so the next number is
taken from ``max(login.json, HIGH_WATER_FILENAME) + 1`` and the high-water mark
is advanced before every commit.  Two commits can never reuse a number, so a
gap takeover is always visible as a *newer* generation.

Restart storms: a supervisor (launchd ``KeepAlive``, systemd ``Restart=always``)
relaunches on any exit, so the stale path must never be reachable from a
supervised launch.  The templates never set this variable, the CLI launcher
clears an inherited one, and :func:`take_expected_generation` drops it when the
process is service managed.  The exit code is distinct only so the launcher
can name the outcome; it is not a supervisor contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from hyprial.identity import EXPECTED_IDENTITY_GENERATION_ENV

IDENTITY_GENERATION_KEY = "identityGeneration"
#: Non-secret high-water mark, ``HYPRIAL_HOME/<this>``; survives logout.
HIGH_WATER_FILENAME = ".identity-generation"
IDENTITY_GENERATION_STALE_EXIT_CODE = 76


class IdentityGenerationStale(RuntimeError):
    code = "IDENTITY_TRANSACTION_STALE"

    def __init__(self, expected: int, current: int | None) -> None:
        super().__init__(
            f"identity generation {current!r} on disk is not the expected "
            f"{expected}: a later login committed after this daemon was launched"
        )
        self.expected = expected
        self.current = current


def service_managed(environ: Mapping[str, str]) -> bool:
    """Supervisor-launched (launchd/systemd units set it; so must docker/k8s)."""

    from hyprial.kernel import SERVICE_MANAGED_ENV

    return environ.get(SERVICE_MANAGED_ENV) == "1"


def read_identity_generation(home: Path) -> int | None:
    """The committed generation in ``secrets/login.json``, or None (never raises)."""

    from hyprial.shell.impl.login.flow import credential_path

    try:
        record = json.loads(
            credential_path(hyprial_home=Path(home)).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        return None
    value = record.get(IDENTITY_GENERATION_KEY) if isinstance(record, dict) else None
    if type(value) is int and value > 0:
        return value
    return None


def _read_high_water(home: Path) -> int:
    try:
        record = json.loads((Path(home) / HIGH_WATER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return 0
    value = record.get(IDENTITY_GENERATION_KEY) if isinstance(record, dict) else None
    return value if type(value) is int and value > 0 else 0


def next_identity_generation(home: Path) -> int:
    """Reserve the next generation (call under the identity lock, before commit).

    The high-water mark is advanced first, so a commit that then fails only
    leaves a gap -- never a reused number.
    """

    from hyprial.kernel import atomic_json_write

    value = max(read_identity_generation(home) or 0, _read_high_water(home)) + 1
    atomic_json_write(Path(home) / HIGH_WATER_FILENAME, {IDENTITY_GENERATION_KEY: value})
    return value


def set_launch_generation(
    environment: MutableMapping[str, str], expected: int | None
) -> None:
    """Child env for a CLI-spawned daemon: never forward an inherited value."""

    environment.pop(EXPECTED_IDENTITY_GENERATION_ENV, None)
    if expected is not None:
        environment[EXPECTED_IDENTITY_GENERATION_ENV] = str(expected)


def set_launch_id(environment: MutableMapping[str, str], launch_id: str) -> None:
    """Child env for a CLI-spawned daemon: this launch's nonce, never an inherited one.

    The readiness fence matches daemon.json ``launchId`` against it on every
    platform (``Popen.pid`` is not the daemon's pid behind a Windows venv
    ``python.exe`` redirector); see ``DAEMON_LAUNCH_ID_ENV`` in the kernel
    daemon-launch contract.
    """

    from hyprial.kernel import DAEMON_LAUNCH_ID_ENV

    environment[DAEMON_LAUNCH_ID_ENV] = launch_id


def take_launch_id(environ: MutableMapping[str, str]) -> str | None:
    """Pop the launch nonce so no descendant (worker, harness, updater) inherits it."""

    from hyprial.kernel import DAEMON_LAUNCH_ID_ENV

    value = environ.pop(DAEMON_LAUNCH_ID_ENV, None)
    return value if value else None


def take_expected_generation(environ: MutableMapping[str, str]) -> int | None:
    """Pop the expectation immediately so no descendant inherits it.

    A malformed value can never match a committed generation, so it is
    returned as ``0`` (always stale) instead of being ignored.
    """

    raw = environ.pop(EXPECTED_IDENTITY_GENERATION_ENV, None)
    if raw is None or service_managed(environ):
        return None
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value > 0 else 0


def verify_expected_generation(home: Path, expected: int) -> None:
    current = read_identity_generation(home)
    if current != expected:
        raise IdentityGenerationStale(expected, current)


def superseded_by(home: Path, committed: int | None) -> dict[str, object] | None:
    """Facts when the committed generation is no longer the one on disk.

    ``superseded`` is True only for a strictly newer integer (a later login
    won).  A missing credential (concurrent logout) or any other value is
    reported as changed, not as "a later login committed".
    """

    if committed is None:
        return None
    current = read_identity_generation(home)
    if current == committed:
        return None
    return {
        "superseded": type(current) is int and current > committed,
        "identityGeneration": committed,
        "currentIdentityGeneration": current,
    }


SUPERSEDED_NEXT_STEP = (
    "the later login's identity is in effect; re-run hyprial login to switch "
    "back to this account"
)


def superseded_error(
    error_type: type[Exception],
    home: Path,
    committed: object,
    cause: str,
    data: Mapping[str, object] | None = None,
) -> Exception | None:
    """``IDENTITY_SUPERSEDED`` (last writer wins) when a later commit replaced ours."""

    facts = superseded_by(home, committed if type(committed) is int else None)
    if facts is None:
        return None
    if facts["superseded"] is not True:
        return error_type(  # type: ignore[call-arg]
            "CREDENTIAL_CHANGED",
            "secrets/login.json no longer holds the identity this login committed "
            f"({cause}); it was removed or rewritten by another writer",
            {**(data or {}), **facts},
        )
    return error_type(  # type: ignore[call-arg]
        "IDENTITY_SUPERSEDED",
        f"a later login committed a newer identity after this one ({cause}); "
        "the later login is in effect",
        {**(data or {}), **facts},
    )
