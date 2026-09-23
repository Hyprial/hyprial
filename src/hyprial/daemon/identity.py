"""Daemon owner settings and target identity policy."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from hyprial import uri as _uri
from hyprial.home import configured_hyprial_home
from hyprial.persistent_config import atomic_json_write


def _settings_path(env: Mapping[str, str], hyprial_home: Path | None) -> Path:
    """The ``settings.json`` this resolution reads, from the same env mapping.

    Mirrors ``hyprial.home.configured_hyprial_home`` deliberately rather than calling
    it: that helper reads ``os.environ`` directly, and this resolution must be
    drivable from an explicit mapping so the source order stays testable
    without touching process state.
    """

    if hyprial_home is not None:
        return Path(hyprial_home) / "settings.json"
    home = configured_hyprial_home(env)[0]
    return home / "settings.json"


def _settings_owner(path: Path) -> str | None:
    """``settings.json`` ``owner``, or ``None`` when the file or key is absent.

    A malformed file fails loudly rather than falling through to the
    "identity is not set" error: those are different operator situations and
    collapsing them would hide a typo behind an init instruction.
    """

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    owner = record.get("owner")
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError(f"{path} owner must be a non-empty string; got {owner!r}")
    return owner.strip()


def node_owner_or_none(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> str | None:
    """Optional node identity with the same policy as resolve_node_owner.

    Only an absent file/key yields None. Malformed or invalid identity remains
    an error. Environment precedence/normalization and settings parsing live
    here once, so naming validation and acting identity cannot drift apart.
    """
    env = os.environ if environ is None else environ
    owner = env.get("HYPRIAL_OWNER", "").strip()
    if not owner:
        owner = _settings_owner(_settings_path(env, hyprial_home))
    if owner is not None and ":" in owner:
        raise ValueError(f"owner must not contain ':'; got {owner!r}")
    return owner


def resolve_node_owner(
    environ: Mapping[str, str] | None = None,
    *,
    hyprial_home: Path | None = None,
) -> str:
    """The **user identity** this daemon mints into agent/adapter URIs.

    Source order: ``HYPRIAL_OWNER`` > ``settings.json`` ``owner`` > loud
    failure.  ``settings.json`` is the one landing spot.  The manual
    pre-login owner flags were removed with U5 (D-U2-5); the ways in are
    exactly three: ``hyprial login`` (the Hyprial service, login U2), the
    self-host branch of ``hyprial init`` (U6, 2026-09-18: the owner is
    asserted by the host's own tailnet via ``tailscale whoami`` — no manual
    entry), and the ``HYPRIAL_OWNER`` override below.

    ⭐ D7, pinned by U5: ``HYPRIAL_OWNER`` is an **override for tests and
    managed deployments only**.  It outranks settings for the daemon, so a
    ``hyprial login`` whose identity differs from a non-empty override errors
    **before writing anything** — writing settings would not change the
    daemon's real identity.

    ⚠️ Both surfaces below must keep naming **real** commands.
    ``test_identity_guidance.py`` asserts every command named **in this
    docstring and in the failure message below** exists -- it reads both
    surfaces, so adding a command here puts it under the check too.  See
    that test before rewording.

    ⛔ There is deliberately **no local-login fallback**.  This function used
    to end in ``getpass.getuser()``, which returns the *host* login and so
    made the owner segment a property of the machine rather than of the
    person — the exact defect this signature exists to remove.  The owner
    segment is a user identity: **one value across all of a user's machines**,
    with ``<machine>`` doing the per-node work.

    ⚠️ Consequence, and it is load-bearing: because owner is now identical
    across a user's machines, ``<owner>`` alone no longer distinguishes
    nodes.  Anything asking "is this actor mine?" must compare ``<machine>``
    as well — see ``_resolve_agent_alias``, which this change would otherwise
    have made ambiguous for every actor name the peer also uses.
    """

    env = os.environ if environ is None else environ
    owner = node_owner_or_none(env, hyprial_home=hyprial_home)
    if owner is None:
        path = _settings_path(env, hyprial_home)
        raise ValueError(
            "user identity is not set: HYPRIAL_OWNER is unset or empty, and "
            f"{path} has no 'owner'. Run: hyprial login, or hyprial init to "
            "choose a self-hosted tailscale identity (or set HYPRIAL_OWNER)"
        )
    return owner


def write_settings_owner(
    owner: str,
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> Path:
    """Persist ``owner`` into ``settings.json``, and return the file written.

    Idempotent by construction: the file is read, one key is replaced, and the
    whole record is written back, so re-running with the same value is a no-op
    and every unrelated key survives.  ⛔ Not a blind overwrite -- clobbering a
    settings file to set one field is how an operator loses the rest of it.

    Rejects the same shapes :func:`resolve_node_owner` rejects, and rejects
    them *here* rather than at the next daemon start: a value that cannot be
    resolved must not reach disk, or the failure surfaces one boot later with
    nothing pointing back at the command that caused it.

    ⚠️ A *different* existing owner is **overwritten, not refused**, and that
    is a decision rather than an oversight.  hyprial is unreleased and a node has
    exactly one identity, so re-running with a new value is how an operator
    corrects a typo -- refusing would leave the only repair path outside the
    tool (hand-editing the file), which is the situation this option exists to
    end.  ⇒ Revisit if a node ever legitimately holds two identities.
    """

    value = owner.strip()
    if not value:
        raise ValueError("owner must not be empty")
    if ":" in value:
        raise ValueError(f"owner must not contain ':'; got {owner!r}")
    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    record["owner"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def read_settings_identity(
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> tuple[str, str | None, str | None] | None:
    """Return the persisted owner/mode/issuer tuple exactly as committed."""

    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    owner = record.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return None
    mode = record.get("identityMode")
    issuer = record.get("identityIssuer")
    return (
        owner.strip(),
        mode if isinstance(mode, str) else None,
        issuer if isinstance(issuer, str) else None,
    )


def read_settings_identity_metadata(
    *,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> tuple[str | None, str | None]:
    """Return the configured mode/issuer without inventing legacy metadata."""

    identity = read_settings_identity(environ=environ, hyprial_home=hyprial_home)
    return (identity[1], identity[2]) if identity is not None else (None, None)


def write_settings_identity(
    owner: str,
    *,
    mode: str,
    issuer: str | None,
    environ: Mapping[str, str] | None = None,
    hyprial_home: Path | None = None,
) -> Path:
    """Atomically commit owner/mode/issuer as one settings record.

    This is the S3 login commit path.  ``write_settings_owner`` remains the
    compatibility writer for older direct callers; a coordinated login must
    not expose a new owner with stale mode metadata between writes.
    """

    value = owner.strip()
    if not value or ":" in value:
        raise ValueError(f"owner must be non-empty and contain no ':'; got {owner!r}")
    if mode not in {"casdoor", "local-usage", "tailscale-selfhost"}:
        raise ValueError(f"unsupported identity mode {mode!r}")
    if mode == "casdoor" and (not isinstance(issuer, str) or not issuer.strip()):
        raise ValueError("casdoor identity requires a non-empty issuer")
    if mode in {"local-usage", "tailscale-selfhost"} and issuer not in {None, ""}:
        # local-usage keeps no issuer because it is authenticated by nobody;
        # tailscale-selfhost (U6) is asserted by the host's own tailnet
        # control plane, which has no OIDC issuer to name.
        raise ValueError(f"{mode} identity must not retain an issuer")
    env = os.environ if environ is None else environ
    path = _settings_path(env, hyprial_home)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    desired_issuer = issuer.strip() if isinstance(issuer, str) else None
    if (
        record.get("owner") == value
        and record.get("identityMode") == mode
        and record.get("identityIssuer") == desired_issuer
    ):
        return path
    record["owner"] = value
    record["identityMode"] = mode
    record["identityIssuer"] = desired_issuer
    atomic_json_write(path, record)
    return path


# -- Target classification (Allen's four object types) ----------------------
#
# 主机 (host), user, adapter (channel route), connector (agent) are four
# different object types.  Listing a node as ``targetKind: agent`` told every
# caller "you can send this an actor message": the send normalized back to
# the bare node id, landed in the node inbox with a signed receipt, and no
# consumer ever fetched it -- a delivered-and-receipted silent hole.  The
# classifier below is the single authority the targets view, the admission
# gate in ``_ensure_interactive_route``, and the CLI/MCP ``--kind`` filters
# all share, so the four types can never drift apart again.

#: Kinds a sender can actually deliver an actor message to.  ``host`` and
#: ``unknown`` rows are observability, never advertisement.
DELIVERABLE_TARGET_KINDS = frozenset(
    {
        _uri.TARGET_KIND_AGENT,
        _uri.TARGET_KIND_USER,
        _uri.TARGET_KIND_CHANNEL_ROUTE,
    }
)


def normalize_agent_recipient(target: str) -> str:
    """Resolve the ``agent:<node>`` display form to the bare node name.

    ``hyprial targets`` used to advertise node targets as ``agent:<node>`` so
    the kind looked explicit, but liveliness, outbox records, and delivery
    keys all use the bare node name.  Normalizing here — before the value
    reaches the inbox service — keeps one persisted recipient form so
    dedup, custody, and the DLQ never split.  Canonical interactive actor
    URIs (``agent:<owner>:<machine>:<agent>``) are real identities
    registered in presence, not display decoration, and pass through
    untouched, as do ``user:`` targets.
    """

    if target.startswith(_uri.AGENT_URI_PREFIX):
        remainder = target.removeprefix(_uri.AGENT_URI_PREFIX)
        if remainder and ":" not in remainder:
            return remainder
    return target


def classify_target_identity(value: str) -> str:
    """Classify one network identity into Allen's four object types.

    Rules, in order:

    * ``agent:<owner>:<machine>:<actor>`` (exactly four non-empty segments)
      is a connector -- ``agent``.
    * ``agent:<name>`` (single segment) and bare names with no ``:`` are
      node-id-shaped: every daemon announces its bare ``node_id`` under the
      actor liveliness keyspace, and pre-canonical sessions could persist
      the same shape.  Neither is a verified connector, so both downgrade
      to ``host`` -- never advertised as an agent, never rejected.
    * ``user:<owner>`` is a human recipient -- ``user``.
    * ``route:<adapter>:<route>`` is an adapter-mediated address --
      ``channel_route``.  (``channel:<adapter>:<name>`` reply-bridge
      addresses are NOT delivery targets -- sends to them are rejected --
      so they classify ``unknown`` here, not ``channel_route``.)
    * Everything else (``agent:`` with an empty name, three/five-segment
      ``agent:`` shapes, empty segments, unknown schemes, the empty
      string) is ``unknown``.  Unknown is a deliberate, test-covered
      verdict: when in doubt, downgrade the label, do not reject.
    """

    if not value or not value.strip():
        return _uri.TARGET_KIND_UNKNOWN
    if ":" not in value:
        return _uri.TARGET_KIND_HOST
    scheme, _, remainder = value.partition(":")
    if scheme == "agent":
        if not remainder:
            return _uri.TARGET_KIND_UNKNOWN
        if ":" not in remainder:
            return _uri.TARGET_KIND_HOST
        if _uri.parse_agent_uri(value) is not None:
            return _uri.TARGET_KIND_AGENT
        return _uri.TARGET_KIND_UNKNOWN
    if scheme == "user":
        if remainder and ":" not in remainder:
            return _uri.TARGET_KIND_USER
        return _uri.TARGET_KIND_UNKNOWN
    if scheme == "route":
        parts = value.split(":")
        if len(parts) == 3 and all(parts[1:]):
            return _uri.TARGET_KIND_CHANNEL_ROUTE
        return _uri.TARGET_KIND_UNKNOWN
    return _uri.TARGET_KIND_UNKNOWN


def target_deliverable(kind: str) -> bool:
    """Can a sender deliver an actor message to a target of this kind?"""

    return kind in DELIVERABLE_TARGET_KINDS
