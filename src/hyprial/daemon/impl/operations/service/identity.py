"""Per-home service identity: unit names, unit paths, and home ownership.

Z-8b decisions (owner-confirmed 2026-11-15, PR #2830):
- The default home (realpath(HYPRIAL_HOME) == realpath(HOME/.hyprial)) keeps
  the fixed com.hyprial.hyprial.daemon / hyprial.service identity and unit
  paths byte-identical.
- Any other home inside the operating home derives com.hyprial.hyprial.daemon.<id>
  / hyprial-<id>.service, where <id> is the first 12 lowercase hex digits of
  sha256 over realpath(HYPRIAL_HOME) encoded with os.fsencode (the path's
  UTF-8 filesystem bytes; no Unicode normalization).
- Canonicalization is realpath only: symlinks, trailing slashes and "." segments
  collapse, but the case of existing path components is kept verbatim (measured
  on macOS 2026-11-15: realpath('/users/x') stays lowercase). Two spellings of
  one case-insensitive directory therefore produce two ids and two units; the
  same home must always be spelled the same way. The 48-bit truncation keeps
  per-user collision risk negligible.
- The former service-home-nondefault refusal is removed; only the
  outside-operating-home containment remains here (audit G-M1/C1, L3).

Z-8 launchd acceptance fix D1 (2026-11-16, PR #2830): the program a unit runs
is normalised with ``os.path.abspath`` only, never ``resolve()``. A venv's
``bin/python`` is a symlink to the base CPython; resolving it renders an
interpreter with no site-packages, so the unit crash-looped on
``ModuleNotFoundError``. ``program_path`` is the single normaliser for the
template render, the attribution check and the launchd job scanner, so render
and compare cannot disagree.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from hyprial.kernel import current_uid

from .types import (
    DEFAULT_SYSTEMD_UNIT,
    LAUNCHD_LABEL,
    ServiceConfig,
    ServiceOwnershipError,
    ServicePlatform,
)

UNIT_ID_LENGTH = 12


def home_unit_id(hyprial_home: Path) -> str:
    """The first 12 hex digits of sha256 over the canonical home path bytes."""

    resolved = os.path.realpath(hyprial_home)
    return hashlib.sha256(os.fsencode(resolved)).hexdigest()[:UNIT_ID_LENGTH]


def program_path(config: ServiceConfig) -> Path:
    """The absolute, symlink-preserving program path a unit renders and matches."""

    return Path(os.path.abspath(os.path.expanduser(config.executable)))


def is_default_home(config: ServiceConfig) -> bool:
    """Whether this config serves the default $HOME/.hyprial home."""

    return os.path.realpath(config.hyprial_home) == os.path.realpath(
        config.home / ".hyprial"
    )


def launchd_label(config: ServiceConfig) -> str:
    """This home's launchd label: fixed for the default home, derived otherwise."""

    if is_default_home(config):
        return LAUNCHD_LABEL
    return f"{LAUNCHD_LABEL}.{home_unit_id(config.hyprial_home)}"


def systemd_unit(config: ServiceConfig) -> str:
    """This home's systemd user unit: fixed for the default home, derived otherwise."""

    if is_default_home(config):
        return DEFAULT_SYSTEMD_UNIT
    return f"hyprial-{home_unit_id(config.hyprial_home)}.service"


def unit_path(platform: ServicePlatform, config: ServiceConfig, service_home: Path) -> Path:
    """The unit file this home owns on the given supervisor."""

    if platform == "launchd":
        return (
            service_home
            / "Library"
            / "LaunchAgents"
            / f"{launchd_label(config)}.plist"
        )
    return service_home / ".config" / "systemd" / "user" / systemd_unit(config)


def require_service_home(config: ServiceConfig, service_home: Path) -> None:
    """A supervisor unit is only ever written inside the operating home."""

    operating = Path(os.path.realpath(service_home))
    home = Path(os.path.realpath(config.hyprial_home))
    if home != operating and not home.is_relative_to(operating):
        raise ServiceOwnershipError(
            f"refusing to manage a supervisor unit for HYPRIAL_HOME {home} "
            f"outside the operating home {operating}",
            reason="hyprial-home-outside-operating-home",
        )


def require_account_home(config: ServiceConfig, service_home: Path) -> None:
    """Changing HOME must not reach another account's per-user supervisor."""

    import pwd

    account_home = Path(pwd.getpwuid(current_uid()).pw_dir).resolve()
    if service_home.resolve() != account_home or config.home.resolve() != account_home:
        raise ServiceOwnershipError(
            "HOME does not match the supervisor account home; "
            "changing HOME does not isolate the per-user service manager",
            reason="service-account-home-mismatch",
        )
