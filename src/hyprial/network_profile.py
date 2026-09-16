"""Network profiles: **one profile = one ``HYPRIAL_HOME``** (hyprial login U1).

A *network profile* is the small, non-secret record that says which overlay
network a home belongs to: the organization name, the identity issuer, the
control plane (``tailscale`` or ``headscale``) with its URL, and how nodes
join.  The built-in profile uses Hyprial's self-hosted Headscale deployment
(Allen 2026-09-13); ``tailscale`` remains available to explicit profiles.  It
lives in ``$HYPRIAL_HOME/profile.json`` — and **a home without that
file is the built-in ``hyprial`` profile**, which is exactly why the existing
production ``~/.hyprial`` needs no migration (spec §1.1, design §9.8):

    $HYPRIAL_HOME/
        profile.json      this record (non-secret, see :data:`NetworkProfile`)
        settings.json     ``owner`` — the identity landing spot this module
                          never touches (``daemon/identity.py`` owns it)
        secrets/          tokens (0600) — **U2 writes this, U1 only names it**
        state/tsnet/      tsnet node state — **U3 writes this, U1 only names
                          it** (see :data:`SECRETS_DIRNAME`,
                          :data:`TSNET_STATE_DIRNAME`)
        profiles/<name>/  other profiles' homes, each a complete HYPRIAL_HOME

Multi-profile = multi-home; the *only* selection mechanism is ``HYPRIAL_HOME``.
There is deliberately no ``HYPRIAL_PROFILE`` variable and no machine-level
"current profile" pointer: the resident daemon's home is pinned by its
service unit's ``HYPRIAL_HOME``, and a second selector would let the CLI point
at home A while the daemon runs in home B.

Naming landmine (spec §2): the word "profile" already exists in this
repository meaning the host-side Squire user dossier (``UserProfileStore``,
``state/users.json``).  Everything here is a **NetworkProfile**; the two have
nothing to do with each other, one home holds exactly one owner and one
network profile, and nothing here reads or writes ``users.json``.

DEFAULT_PROFILE
    The built-in ``hyprial`` record, returned by :func:`resolve_profile` for
    any home that has no ``profile.json``.  Its issuer domain literal 在整个
    src/ 树里唯一允许出现的地方就是 DEFAULT_PROFILE 这一处:U2 起所有代码
    一律读 ``profile.issuer``,不得另抄字面量——判据测试
    (tests/test_network_profile.py 的 T9)会扫描全 src/ 并锚定本段,注记与
    绊线互指,两个都不能单独消失。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from hyprial.home import configured_hyprial_home

__all__ = [
    "CONTROL_PLANE_KINDS",
    "DEFAULT_PROFILE",
    "JOIN_MODES",
    "NetworkProfile",
    "PROFILE_FILENAME",
    "PROFILE_VERSION",
    "SECRETS_DIRNAME",
    "SOURCE_BUILT_IN",
    "SOURCE_PROFILE_FILE",
    "TSNET_STATE_DIRNAME",
    "profile_path",
    "read_profile",
    "resolve_profile",
    "validate_profile",
    "write_profile",
]

PROFILE_FILENAME = "profile.json"
"""Basename of the profile record inside an ``HYPRIAL_HOME``."""

PROFILE_VERSION = 1
"""The only schema version this module reads or writes (unknown ⇒ loud)."""

SECRETS_DIRNAME = "secrets"
"""Where U2 will land tokens (``$HYPRIAL_HOME/secrets/``, 0600).  U1 names the
location for U2 and never creates or writes it."""

TSNET_STATE_DIRNAME = "state/tsnet"
"""Where U3 will land tsnet node state.  U1 names the location and never
creates or writes it."""

CONTROL_PLANE_KINDS: Sequence[str] = ("tailscale", "headscale")
JOIN_MODES: Sequence[str] = ("interactive", "preauthkey")

SOURCE_PROFILE_FILE = "profile.json"
SOURCE_BUILT_IN = "built-in default"
"""The two ``resolve_profile`` source labels (spec §1.3)."""


@dataclass(frozen=True, slots=True)
class NetworkProfile:
    """One overlay network's non-secret coordinates (spec §1.2).

    Only names and URLs — no derived URIs, no tokens, no endpoints fetched
    from discovery.  ``issuer`` is stored and returned **verbatim** (no
    normalization): U2 requires discovery's issuer to equal it byte for
    byte, so any "helpful" rewriting here would corrupt that comparison.

    ``client_id`` is the public OIDC client id ``hyprial login`` presents to
    that issuer (D-U2-1) — public, not a secret, fine to commit; the
    built-in default carries app-hyprial's real id below.
    """

    org: str
    issuer: str
    client_id: str
    control_plane_kind: str
    control_plane_url: str
    join: str

    def as_record(self) -> dict[str, object]:
        """The ``profile.json`` mapping for this profile (schema v1)."""

        return {
            "version": PROFILE_VERSION,
            "org": self.org,
            "issuer": self.issuer,
            "clientId": self.client_id,
            "controlPlane": {
                "kind": self.control_plane_kind,
                "url": self.control_plane_url,
            },
            "join": self.join,
        }


# Built-in control plane = Hyprial's self-hosted Headscale (Allen 2026-09-13).
# The ``tailscale`` kind remains supported for explicit profiles; changing the
# built-in selection is this constant and nothing else.
DEFAULT_PROFILE = NetworkProfile(
    org="hyprial",
    issuer="https://auth.hyprial.com",
    client_id="2cdd1defc1a0e219fafb",
    control_plane_kind="headscale",
    control_plane_url="https://head.hyprial.ai",
    join="interactive",
)


def profile_path(env: Mapping[str, str], hyprial_home: Path | None = None) -> Path:
    """The ``profile.json`` this home's resolution reads, from one env mapping.

    Mirrors ``daemon.identity._settings_path`` deliberately (same home rule:
    explicit ``hyprial_home`` > ``HYPRIAL_HOME`` > ``~/.hyprial``) rather than calling a
    helper that reads ``os.environ`` directly, so both files always resolve
    from the same home under the same mapping and stay testable without
    touching process state.  T5 pins the two parents together.
    """

    if hyprial_home is not None:
        return Path(hyprial_home) / PROFILE_FILENAME
    home = configured_hyprial_home(env)[0]
    return home / PROFILE_FILENAME


def resolve_profile(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> tuple[NetworkProfile, str]:
    """Resolve the home's profile: ``(profile, source)``.

    Missing file ⇒ ``(DEFAULT_PROFILE, "built-in default")`` — the default is
    *absence of the file*, never a fallback for a bad one.  A file that is
    unreadable, unparseable, or fails :func:`validate_profile` raises
    ``ValueError`` naming the path and the reason: "file is broken" and
    "file is not there" are different operator situations, the same
    loud-failure分寸 as ``daemon.identity._settings_owner``.
    """

    env = os.environ if environ is None else environ
    path = profile_path(env, hyprial_home)
    try:
        return read_profile(path), SOURCE_PROFILE_FILE
    except FileNotFoundError:
        # Only the file's *absence* means the default; anything wrong with a
        # file that exists raises ValueError from read_profile below's stack
        # — never a fallback (same分寸 as identity._settings_owner).
        return DEFAULT_PROFILE, SOURCE_BUILT_IN


def read_profile(path: Path) -> NetworkProfile:
    """Load and validate the record at ``path``; loud on every defect."""

    return _profile_from_record(_load_record(path), path)


def validate_profile(profile: NetworkProfile, *, source: Path | str) -> None:
    """Apply every schema-v1 rule (spec §1.2) or raise ``ValueError``.

    All failures are loud — this module never falls back to the default for
    a record that exists but is wrong.  ``source`` is only the path named in
    error messages.
    """

    where = str(source)
    _check_org(profile.org, where)
    _check_https_url(profile.issuer, "issuer", where)
    _check_client_id(profile.client_id, where)
    if profile.control_plane_kind not in CONTROL_PLANE_KINDS:
        raise ValueError(
            f"{where}: controlPlane.kind must be one of "
            f"{list(CONTROL_PLANE_KINDS)}; got {profile.control_plane_kind!r}"
        )
    _check_https_url(profile.control_plane_url, "controlPlane.url", where)
    if profile.join not in JOIN_MODES:
        raise ValueError(
            f"{where}: join must be one of {list(JOIN_MODES)}; "
            f"got {profile.join!r}"
        )
    # join='preauthkey' is the unattended mode and is valid with either kind
    # (ruled 2026-09-04): with Headscale the key is minted per user; with the
    # official control plane it is a *tagged*, ephemeral, pre-approved key
    # (CI's ``tag:hyprial-ci``) so the node belongs to the tag, not to a person.
    # The earlier "preauthkey requires headscale" rule was retired with that
    # ruling; no combination is a configuration error here.


def write_profile(
    profile: NetworkProfile,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> Path:
    """Validate ``profile`` and write the **whole record**; returns the path.

    Idempotent by bytes: the payload is a deterministic dump of exactly this
    record, so writing the same profile twice leaves the file byte-identical
    (spec T6).  The whole file is this module's record and nothing else's —
    unlike ``settings.json`` (read-modify-write to preserve unrelated keys),
    ``profile.json`` has no other writers, so an integral rewrite is the
    honest shape.  It never touches ``settings.json``.  A profile that would
    not validate never reaches disk.
    """

    env = os.environ if environ is None else environ
    path = profile_path(env, hyprial_home)
    validate_profile(profile, source=path)
    payload = json.dumps(profile.as_record(), indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(payload, encoding="utf-8")
    return path


# -- internals ----------------------------------------------------------------


_TOP_LEVEL_KEYS = frozenset(
    {"version", "org", "issuer", "clientId", "controlPlane", "join"}
)
_CONTROL_PLANE_KEYS = frozenset({"kind", "url"})


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json.loads`` hook: a repeated key anywhere is a rejected record.

    Duplicate keys are exactly how "two issuers" would try to appear (spec
    T3) — and silently keeping the last one is the failure mode the rule
    exists to prevent, so both the top level and nested objects go through
    this hook.
    """

    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key in profile record: {key!r}")
        seen.add(key)
    return dict(pairs)


def _load_record(path: Path) -> dict[str, object]:
    """Read and parse ``path``; loud on everything except absence."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Absence is not a defect: the caller decides whether a missing
        # record means "built-in default" (resolve) or "nothing to compare"
        # (create).  Must be caught *before* the OSError branch below —
        # FileNotFoundError is an OSError.
        raise
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    try:
        record = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    except ValueError as error:  # the duplicate-key hook above
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    return record


def _profile_from_record(
    record: Mapping[str, object], path: Path
) -> NetworkProfile:
    """Validate the parsed mapping and build the profile from it."""

    version = record.get("version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != PROFILE_VERSION
    ):
        raise ValueError(
            f"{path}: version must be {PROFILE_VERSION}; got {version!r}"
        )
    unknown = sorted(set(record) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(
            f"{path}: unknown top-level key(s) {unknown}; profile.json is a "
            "small closed record — an extra key is usually a misspelled one"
        )
    control = record.get("controlPlane")
    if not isinstance(control, dict):
        raise ValueError(f"{path}: controlPlane must be an object; got {control!r}")
    unknown_control = sorted(set(control) - _CONTROL_PLANE_KEYS)
    if unknown_control:
        raise ValueError(
            f"{path}: unknown controlPlane key(s) {unknown_control}"
        )
    profile = NetworkProfile(
        org=record.get("org"),  # type: ignore[arg-type]
        issuer=record.get("issuer"),  # type: ignore[arg-type]
        client_id=record.get("clientId"),  # type: ignore[arg-type]
        control_plane_kind=control.get("kind"),  # type: ignore[arg-type]
        control_plane_url=control.get("url"),  # type: ignore[arg-type]
        join=record.get("join"),  # type: ignore[arg-type]
    )
    validate_profile(profile, source=path)
    return profile


def _check_org(value: object, where: str) -> None:
    """Non-empty, no ``:`` and no ``/`` — it becomes a path segment."""

    if (
        not isinstance(value, str)
        or not value
        or ":" in value
        or "/" in value
    ):
        raise ValueError(
            f"{where}: org must be a non-empty string without ':' or '/'; "
            f"got {value!r}"
        )


def _check_client_id(value: object, where: str) -> None:
    """Non-empty string without whitespace (D-U2-1: required, public).

    ``clientId`` is the OIDC client id ``hyprial login`` presents to the
    profile's issuer.  It is a *public* value — not a secret — so the check
    is only "usable as a form value", no format guessing: non-empty, no
    leading/trailing whitespace, no embedded whitespace.
    """

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(
            f"{where}: clientId must be a non-empty string without whitespace "
            f"(D-U2-1: the public OIDC client id); got {value!r}"
        )


def _check_https_url(value: object, field: str, where: str) -> None:
    """Non-empty ``https://`` URL, no trailing ``/``, no query or fragment.

    Applied verbatim to ``issuer`` (spec §1.2 rule 2) and, for the same
    hygiene, to ``controlPlane.url``: both are base URLs other code will
    concatenate onto, and both are read back exactly as written — rejecting
    an ambiguous form loudly beats normalizing it silently.
    """

    if not isinstance(value, str) or not value.startswith("https://") or value == "https://":
        raise ValueError(
            f"{where}: {field} must be a non-empty https:// URL; got {value!r}"
        )
    if value.endswith("/"):
        raise ValueError(f"{where}: {field} must not end with '/'; got {value!r}")
    if "?" in value or "#" in value:
        raise ValueError(
            f"{where}: {field} must not contain a query or fragment; "
            f"got {value!r}"
        )
