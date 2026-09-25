"""hyprial's own worker proxy setting, applied per model vendor.

Why this exists (2026-09-25): the production daemon was restarted from a shell
that exported only ``all_proxy``.  Workers inherit the daemon's environment,
codex reads only ``HTTP(S)_PROXY``, and every codex/pi worker launched after
that restart had no route to its model API (one sat 40 minutes in SYN_SENT to
DNS-poisoned addresses).  Claude workers hid the problem because
``~/.claude/settings.json`` injects proxies itself; codex has no config-file
proxy for its model traffic and pi has only one global ``httpProxy``.  So the
worker's route cannot be left to whatever shell happened to start the daemon.

Allen's ruling (Feishu DM, 2026-09-25): a hyprial proxy setting of its own,
applied per model in the matrix -- only OpenAI/Anthropic models get the
proxy.  Concretely:

* ``$HYPRIAL_HOME/settings.json`` carries
  ``{"workerProxy": {"url": ..., "vendors": [...], "noProxy": ...}}``;
* a worker whose model-vendor family is in ``vendors`` gets ``url`` as every
  proxy variable; any other worker gets NO proxy variable at all, so an
  ambient proxy cannot send a domestic vendor's traffic abroad;
* with no ``url`` configured the ambient behaviour stays (plus the
  ``all_proxy`` derivation in :mod:`hyprial.agents.environment`).

The setting is read at every worker launch -- never cached -- so a change
applies to the next launch without a daemon restart.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "ANTHROPIC_VENDOR_FAMILY",
    "DEFAULT_WORKER_PROXY_VENDORS",
    "OPENAI_VENDOR_FAMILY",
    "WORKER_PROXY_FIELDS",
    "WORKER_PROXY_KEY_UNKNOWN",
    "WORKER_PROXY_SETTINGS_INVALID",
    "WORKER_PROXY_SETTINGS_KEY",
    "WORKER_PROXY_URL_INVALID",
    "WORKER_PROXY_VENDORS_INVALID",
    "WorkerProxyError",
    "WorkerProxyRoute",
    "WorkerProxySetting",
    "ambient_proxy_absent",
    "launch_worker_proxy_route",
    "model_vendor_family",
    "read_worker_proxy",
    "worker_proxy_route",
    "write_worker_proxy_field",
]

#: The section in settings.json.  camelCase like its siblings ``autoUpgrade``
#: and ``forwarding``; "worker" because it governs the environment hyprial
#: hands its workers, not the daemon's own traffic.
WORKER_PROXY_SETTINGS_KEY = "workerProxy"
#: The section's fields, in the order ``hyprial config set`` documents them.
#: ``noProxy`` mirrors the ``NO_PROXY`` variable it fills.
WORKER_PROXY_URL_FIELD = "url"
WORKER_PROXY_VENDORS_FIELD = "vendors"
WORKER_PROXY_NO_PROXY_FIELD = "noProxy"
WORKER_PROXY_FIELDS = (
    WORKER_PROXY_URL_FIELD,
    WORKER_PROXY_VENDORS_FIELD,
    WORKER_PROXY_NO_PROXY_FIELD,
)

OPENAI_VENDOR_FAMILY = "openai"
ANTHROPIC_VENDOR_FAMILY = "anthropic"
#: Who uses the proxy when ``vendors`` is not configured: the two vendors
#: Allen named.  Every other vendor (deepseek, zai, kimi, ...) is reachable
#: directly and must not be routed through the proxy.
DEFAULT_WORKER_PROXY_VENDORS = (OPENAI_VENDOR_FAMILY, ANTHROPIC_VENDOR_FAMILY)

#: The only schemes every worker client accepts in ``HTTP(S)_PROXY``.  A
#: SOCKS URL is refused rather than written: codex and several Node clients
#: reject it there, which would silently recreate the incident.
WORKER_PROXY_URL_SCHEMES = ("http", "https")

#: The model vendor a harness talks to when the launch names none: the
#: subscription path of each native CLI.  Any other harness without an
#: explicit model vendor has no knowable vendor (pi falls back to its own
#: configured default, jev/dsh/lark/user-proxy are not matrix models).
_NATIVE_VENDOR_FAMILY = {
    "codex": OPENAI_VENDOR_FAMILY,
    "claude": ANTHROPIC_VENDOR_FAMILY,
}
#: pi names OpenAI's subscription route ``openai-codex`` (and may grow
#: suffixed variants); it is the same vendor family as ``openai``.
_OPENAI_SUBSCRIPTION_PREFIX = "openai-codex"

_VENDOR_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Named error codes (surfaced by ``hyprial config set`` and worker launch).
WORKER_PROXY_URL_INVALID = "WORKER_PROXY_URL_INVALID"
WORKER_PROXY_VENDORS_INVALID = "WORKER_PROXY_VENDORS_INVALID"
WORKER_PROXY_SETTINGS_INVALID = "WORKER_PROXY_SETTINGS_INVALID"
WORKER_PROXY_KEY_UNKNOWN = "WORKER_PROXY_KEY_UNKNOWN"


class WorkerProxyError(ValueError):
    """A refused ``workerProxy`` value; ``code`` names which rule failed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class WorkerProxySetting:
    """A configured ``workerProxy``: present only when it has a ``url``."""

    url: str
    vendors: tuple[str, ...] = DEFAULT_WORKER_PROXY_VENDORS
    no_proxy: str | None = None

    def to_json(self) -> dict[str, Any]:
        """The read-surface form; a password in the URL is never echoed."""

        return {
            WORKER_PROXY_URL_FIELD: _display_url(self.url),
            WORKER_PROXY_VENDORS_FIELD: list(self.vendors),
            WORKER_PROXY_NO_PROXY_FIELD: self.no_proxy,
        }


@dataclass(frozen=True, slots=True)
class WorkerProxyRoute:
    """What one worker launch does with the proxy variables.

    ``url`` set: every proxy name carries it.  ``url`` None: every proxy
    name is removed, so the worker connects directly even when the daemon's
    own environment holds a proxy.  (No route at all -- ``None`` where a
    route is expected -- means the ambient behaviour.)
    """

    url: str | None
    no_proxy: str | None = None


def _display_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
    return parts._replace(netloc=netloc).geturl()


def _checked_url(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise WorkerProxyError(
            WORKER_PROXY_URL_INVALID, f"{where} must be a non-empty URL string"
        )
    url = raw.strip()
    parts = urlsplit(url)
    try:
        parts.port  # noqa: B018 - a malformed port raises here, not later
    except ValueError as error:
        raise WorkerProxyError(
            WORKER_PROXY_URL_INVALID, f"{where} has an invalid port: {url!r}"
        ) from error
    if parts.scheme.lower() not in WORKER_PROXY_URL_SCHEMES or not parts.hostname:
        raise WorkerProxyError(
            WORKER_PROXY_URL_INVALID,
            f"{where} must be an {'/'.join(WORKER_PROXY_URL_SCHEMES)} URL "
            f"with a host; got {url!r}",
        )
    return url


def _checked_vendors(raw: Iterable[object], where: str) -> tuple[str, ...]:
    vendors: list[str] = []
    for item in raw:
        name = item.strip().lower() if isinstance(item, str) else None
        if not name or not _VENDOR_NAME.fullmatch(name):
            raise WorkerProxyError(
                WORKER_PROXY_VENDORS_INVALID,
                f"{where} entries must be model-vendor names; got {item!r}",
            )
        if name not in vendors:
            vendors.append(name)
    return tuple(vendors)


def _load_settings(path: Path) -> dict[str, Any] | None:
    """The settings object, None when the file does not exist."""

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise WorkerProxyError(
            WORKER_PROXY_SETTINGS_INVALID, f"cannot parse {path}: {error}"
        ) from error
    if not isinstance(record, dict):
        raise WorkerProxyError(
            WORKER_PROXY_SETTINGS_INVALID,
            f"cannot parse {path}: top level is not an object",
        )
    return record


def read_worker_proxy(hyprial_home: Path) -> WorkerProxySetting | None:
    """The configured ``workerProxy``, or None when it has no ``url``.

    A settings file that exists but does not parse, or a section with a
    malformed value, is an error rather than "unset": falling back to the
    ambient environment would quietly send a domestic vendor through the
    proxy (or OpenAI around it) exactly when the operator's file is damaged.
    The worker launch that reads it fails loudly instead.
    """

    path = Path(hyprial_home) / "settings.json"
    record = _load_settings(path)
    section = None if record is None else record.get(WORKER_PROXY_SETTINGS_KEY)
    if section is None:
        return None
    where = f"{path}: {WORKER_PROXY_SETTINGS_KEY}"
    if not isinstance(section, dict):
        raise WorkerProxyError(
            WORKER_PROXY_SETTINGS_INVALID, f"{where} must be an object"
        )
    if section.get(WORKER_PROXY_URL_FIELD) is None:
        return None
    try:
        url = _checked_url(
            section[WORKER_PROXY_URL_FIELD], f"{where}.{WORKER_PROXY_URL_FIELD}"
        )
        raw_vendors = section.get(WORKER_PROXY_VENDORS_FIELD)
        if raw_vendors is None:
            vendors = DEFAULT_WORKER_PROXY_VENDORS
        elif isinstance(raw_vendors, list):
            vendors = _checked_vendors(
                raw_vendors, f"{where}.{WORKER_PROXY_VENDORS_FIELD}"
            )
        else:
            raise WorkerProxyError(
                WORKER_PROXY_VENDORS_INVALID,
                f"{where}.{WORKER_PROXY_VENDORS_FIELD} must be an array",
            )
    except WorkerProxyError as error:
        raise WorkerProxyError(WORKER_PROXY_SETTINGS_INVALID, str(error)) from error
    no_proxy = section.get(WORKER_PROXY_NO_PROXY_FIELD)
    if no_proxy is not None and not isinstance(no_proxy, str):
        raise WorkerProxyError(
            WORKER_PROXY_SETTINGS_INVALID,
            f"{where}.{WORKER_PROXY_NO_PROXY_FIELD} must be a string",
        )
    return WorkerProxySetting(url, vendors, no_proxy or None)


def write_worker_proxy_field(
    field_name: str, value: str, hyprial_home: Path
) -> tuple[Path, WorkerProxySetting | None]:
    """Persist one ``workerProxy`` field; return the file and the result.

    Read-modify-write like ``write_forwarding_mode``: unrelated keys survive,
    an unparseable file is refused rather than clobbered, and the file is
    replaced atomically.  An empty ``url`` clears the whole setting -- the
    ambient behaviour returns; an empty ``noProxy`` falls back to the
    ambient ``NO_PROXY``; an empty ``vendors`` means no vendor is proxied
    (every worker connects directly).
    """

    if field_name not in WORKER_PROXY_FIELDS:
        raise WorkerProxyError(
            WORKER_PROXY_KEY_UNKNOWN,
            f"unknown {WORKER_PROXY_SETTINGS_KEY} field {field_name!r}; "
            f"supported: {', '.join(WORKER_PROXY_FIELDS)}",
        )
    key = f"{WORKER_PROXY_SETTINGS_KEY}.{field_name}"
    path = Path(hyprial_home) / "settings.json"
    record = _load_settings(path) or {}
    section = record.get(WORKER_PROXY_SETTINGS_KEY)
    section = dict(section) if isinstance(section, dict) else {}
    stripped = value.strip()
    if field_name == WORKER_PROXY_URL_FIELD:
        if not stripped:
            record.pop(WORKER_PROXY_SETTINGS_KEY, None)
            section = None
        else:
            section[WORKER_PROXY_URL_FIELD] = _checked_url(stripped, key)
    elif field_name == WORKER_PROXY_VENDORS_FIELD:
        section[WORKER_PROXY_VENDORS_FIELD] = list(
            _checked_vendors(
                (item for item in stripped.split(",") if item.strip()), key
            )
        )
    elif stripped:
        section[WORKER_PROXY_NO_PROXY_FIELD] = stripped
    else:
        section.pop(WORKER_PROXY_NO_PROXY_FIELD, None)
    if section is not None:
        record[WORKER_PROXY_SETTINGS_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, path)
    return path, read_worker_proxy(hyprial_home)


def model_vendor_family(harness: str, provider: str | None) -> str | None:
    """The ``workerProxy.vendors`` name for one (harness, model vendor) pair.

    ``provider`` is the matrix's model-vendor field (``Candidate.provider``,
    ``HarnessLaunchSpec.model_provider``).  Native codex/claude (no explicit
    vendor) are OpenAI/Anthropic subscriptions; pi's ``openai`` and
    ``openai-codex*`` routes are OpenAI and ``anthropic*`` is Anthropic;
    every other vendor is its own family.  None means the vendor cannot be
    known from the launch (pi on its own default, a non-model harness):
    the caller then keeps the ambient behaviour rather than guessing.
    """

    if provider is None or not provider.strip():
        return _NATIVE_VENDOR_FAMILY.get(harness)
    vendor = provider.strip().lower()
    if vendor == OPENAI_VENDOR_FAMILY or vendor.startswith(
        _OPENAI_SUBSCRIPTION_PREFIX
    ):
        return OPENAI_VENDOR_FAMILY
    if vendor.startswith(ANTHROPIC_VENDOR_FAMILY):
        return ANTHROPIC_VENDOR_FAMILY
    return vendor


def worker_proxy_route(
    setting: WorkerProxySetting | None, vendor_family: str | None
) -> WorkerProxyRoute | None:
    """The route for one launch, or None for the ambient behaviour.

    No setting, or a vendor that cannot be known, keeps today's behaviour.
    A known vendor in ``vendors`` gets the proxy; any other known vendor
    gets every proxy variable removed.
    """

    if setting is None or vendor_family is None:
        return None
    if vendor_family in setting.vendors:
        return WorkerProxyRoute(setting.url, setting.no_proxy)
    return WorkerProxyRoute(None)


def launch_worker_proxy_route(
    hyprial_home: Path, spec: Any
) -> WorkerProxyRoute | None:
    """Read the setting NOW and route one ``HarnessLaunchSpec``.

    Called once per worker launch, so ``hyprial config set workerProxy.*``
    applies to the next launch without a daemon restart.
    """

    return worker_proxy_route(
        read_worker_proxy(hyprial_home),
        model_vendor_family(spec.harness, spec.model_provider),
    )


#: The daemon-environment names that already route a worker somewhere.
_AMBIENT_PROXY_NAMES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def ambient_proxy_absent(
    setting: WorkerProxySetting | None, environ: Mapping[str, str]
) -> bool:
    """Whether model-vendor workers will connect directly with no choice made.

    True only when there is no ``workerProxy`` setting AND the daemon's own
    environment holds no proxy: the exact state of the 2026-09-25 restart.
    """

    return setting is None and not any(environ.get(n) for n in _AMBIENT_PROXY_NAMES)
