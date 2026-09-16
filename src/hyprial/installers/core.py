"""A deliberately small socialware-registry-to-repository installer bridge.

The registry is only a discovery index, not a package store or dependency
resolver.  HYPRIAL owns the trusted source (a release artifact pinned by
sha256), confirmation, invocation boundary, and receipt; the selected
repository owns every application-specific installation step.

Since the release cutover (design-socialware-release-install §4, Allen's ruling)
the *install and upgrade* paths fetch a release tarball over anonymous HTTPS and
verify it against the catalog's sha256 pin -- no git, no token, no tailnet.  The
git face survives only as the read-side validation for **already-installed v1
(git) receipts at launch**, so existing nodes keep starting until they migrate
(§4 P2a "验存量" face; P2b deletes it).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname
from uuid import uuid4

from packaging.version import InvalidVersion, Version

from hyprial.persistent_config import atomic_json_write
from hyprial.updates import git_env


_SCHEMA = "hyprial.install/v1"
#: v2 adds ``commands`` (H3) and reserves ``subscriptions`` (H2).  v1 keeps
#: parsing unchanged -- the installed ``gui`` app ships a v1 manifest today, and
#: rejecting it would break ``hyprial gui`` before the migration that removes it.
_SCHEMA_V2 = "hyprial.install/v2"
_SCHEMAS = (_SCHEMA, _SCHEMA_V2)
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
#: The closed set of verbs a mounted command may take.  Closed on purpose: a
#: free-form string here would let a manifest invent an action the generic
#: runner cannot perform, and the failure would surface at ``hyprial <cmd> <verb>``
#: time instead of at install time.
_COMMAND_ACTIONS = frozenset({"start", "status", "stop", "upgrade"})
#: Top-level keys a v2 manifest may carry.  Anything else is an error: v1→v2
#: is a schema step, not a loosening, and a silently ignored key is exactly how
#: a typo in ``comands`` would mount nothing and report success.
_V2_KEYS = frozenset({"schema", "name", "install", "start", "commands", "subscriptions"})
_INSTALL_ARGV = ("bash", "install.sh")
_GIT_TIMEOUT = 120.0
_INSTALL_TIMEOUT = 1800.0
#: Anonymous HTTPS GET budget for the catalog document and each release
#: artifact.  code.hyprial.com / a public snapshot host can be slow through a
#: tunnel; the number is deliberately generous rather than tuned.
_HTTP_TIMEOUT = 120.0
#: The two ways to point ``code.hyprial.com`` at the internal ssh endpoint when
#: the public tunnel is too slow.  They live here as constants because the same
#: literal text has three readers -- the timeout hint below, README, and the
#: ``hyprial-ops`` skill -- and a test pins all three to these strings.
#:
#: ⚠️ These, and every ``_GIT_*`` constant below, belong to #511 and to the v1
#: git launch face that P2a keeps for existing nodes.  P2b deletes them together
#: with ``_run_git``.  ⛔ P2a does not touch them (spec §约束).
_GIT_SSH_INSTEAD_OF_PERMANENT = (
    'git config --global url."ssh://git@git.internal.hyprial.com/".insteadOf '
    "https://code.hyprial.com/"
)
_GIT_SSH_INSTEAD_OF_ONE_SHOT = (
    "env GIT_CONFIG_COUNT=1 "
    "GIT_CONFIG_KEY_0=url.ssh://git@git.internal.hyprial.com/.insteadOf "
    "GIT_CONFIG_VALUE_0=https://code.hyprial.com/ hyprial install <app> --yes"
)
#: Appended to ``INSTALL_GIT_FAILED`` when git *times out*, and never when git
#: exits non-zero.  ``code.hyprial.com`` is served through a public Cloudflare
#: tunnel (measured 2026-09-14: ~60s to first byte, 217.5s for a shallow fetch)
#: while the same fetch over the internal ssh endpoint takes about a second.
#: This is text only: nothing here rewrites the URL, and an operator-configured
#: ``insteadOf`` already applies because ``git_env()`` inherits the global git
#: config rather than replacing it.
#:
#: ⚠️ The workaround is deliberately framed as an EMERGENCY, not as the way to
#: install: Allen ruled that the tailnet default is the internal ssh endpoint and
#: `feat/installer-tailnet-ssh` implements that.  Saying "instead" here would read
#: as "go configure insteadOf", which is the disposal this text must not give.
_GIT_SSH_WORKAROUND = (
    "Emergency only, inside the tailnet: point code.hyprial.com at the internal ssh "
    "endpoint.  It is a machine-global git rewrite (url.<base>.insteadOf) that changes "
    "every git command on this machine, and the install receipt still records the "
    "catalog address.\n"
    f"  permanent: {_GIT_SSH_INSTEAD_OF_PERMANENT}\n"
    f"  one-shot:  {_GIT_SSH_INSTEAD_OF_ONE_SHOT}"
)
_GIT_TIMEOUT_HINT = (
    "code.hyprial.com is reached through a public tunnel and can be very slow.\n"
    f"{_GIT_SSH_WORKAROUND}\n"
    "If HTTP(S)_PROXY is set, check that NO_PROXY includes code.hyprial.com."
)
#: Substrings git prints when the forge refuses to serve a read.  Anonymous
#: HTTPS gets a 401, which ``GIT_TERMINAL_PROMPT=0`` turns into "could not read
#: Username ... terminal prompts disabled"; rejected credentials get
#: "Authentication failed"; a 403 arrives as "The requested URL returned error:
#: 403"; a rejected ssh key prints "Permission denied (publickey)".  All four
#: were reproduced on this host against a loopback 401/403 (never the tunnel).
#:
#: ⚠️ Near-miss left OUT on purpose: git's generic "Could not read from remote
#: repository" is shared by authentication failures and by a missing repository,
#: so matching it would append this hint to non-authentication failures -- which
#: the contract forbids.  ssh's own authentication failure prints
#: "Permission denied (publickey)", which IS matched.
_GIT_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "returned error: 401",
    "returned error: 403",
    "http basic: access denied",
    "permission denied (publickey)",
)
#: The credential ruling (Allen, 2026-09-14): ``code.hyprial.com`` needs a login to
#: read, and each member configures git with **their own** Forgejo identity -- nothing
#: is handed out centrally and the installer stores no token.  Both halves of the
#: phrase carry the ruling: "Forgejo token" says which kind of credential, "your own"
#: says whose.  The English form is what the hint prints; the Chinese form is what
#: README and the ``hyprial-ops`` skill carry; one test pins both, so a paraphrase
#: cannot quietly drop "your own".
_GIT_OWN_TOKEN_ADVICE = "your own Forgejo token"
_GIT_OWN_TOKEN_ADVICE_ZH = "你自己的 Forgejo token"
#: Appended to ``INSTALL_GIT_FAILED`` when the failure *is* an authentication one.
#: The original text stays in front of it.  ⛔ Nothing here prints or stores a
#: credential, and no token option is added to the installer.  The ssh rewrite stays
#: behind this as the EMERGENCY disposal (``_GIT_SSH_WORKAROUND``); it is not the
#: recommended way to satisfy a missing login.
_GIT_AUTH_HINT = (
    "code.hyprial.com requires a login to read: configure git credentials for this "
    f"machine with {_GIT_OWN_TOKEN_ADVICE} (a credential helper, or the system "
    "keychain).  The installer never prints or stores a token.\n"
    f"{_GIT_SSH_WORKAROUND}"
)
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_ENV = "HYPRIAL_INSTALL_REGISTRY"
#: ⚠️ P0 待裁项① (design §0/§8): the real catalog document URL depends on the
#: A/B/C hosting ruling.  This placeholder carries the *shape* infra-op gave
#: (a GitHub snapshot of ``Hyprial/socialware-registry``); no P2a test asserts
#: its value -- tests override ``HYPRIAL_INSTALL_REGISTRY`` with a file:// or
#: local fixture.  The loader does a plain GET and does not assume raw-vs-release.
_DEFAULT_REGISTRY = (
    "https://raw.githubusercontent.com/Hyprial/socialware-registry/main/catalog-v2.json"
)
#: Catalog / receipt / source-manifest schema identifiers.
_CATALOG_SCHEMA = "hyprial.catalog/v2"
_RECEIPT_SCHEMA_V1 = "hyprial.install-state/v1"
_RECEIPT_SCHEMA_V2 = "hyprial.install-state/v2"
_SOURCE_MANIFEST_SCHEMA = "hyprial.source-manifest/v1"
_SOURCE_MANIFEST_FILE = "source-manifest.json"
#: Where a git catalog entry (rejected in v2) is sent for the developer path.
_DEVELOPER_DOCS = "docs/design-socialware-release-install.md §4"


class InstallError(RuntimeError):
    """Stable failure returned through the public CLI error envelope."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One release-addressable application in the catalog (v2)."""

    name: str
    release: str
    sha256: str
    version: str
    commit: str
    manifest: str


@dataclass(frozen=True, slots=True)
class MountedCommand:
    """One ``hyprial <name>`` subcommand an app asks to have mounted (H3)."""

    name: str
    summary: str
    actions: frozenset[str]


@dataclass(frozen=True, slots=True)
class Manifest:
    name: str
    install: tuple[str, ...]
    start: tuple[str, ...] | None
    commands: tuple[MountedCommand, ...] = ()


@dataclass(frozen=True, slots=True)
class ApplicationLaunch:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    source_commit: str


# --------------------------------------------------------------------------- #
# Catalog v2 (§2): one anonymous HTTPS/file GET of a JSON document             #
# --------------------------------------------------------------------------- #


def _registry_ref() -> str:
    return os.environ.get(_REGISTRY_ENV, _DEFAULT_REGISTRY).strip()


def _registry_is_local(ref: str) -> bool:
    """``file://`` URL or a bare filesystem path -- anything but http(s)."""

    if ref.startswith("file://"):
        return True
    return "://" not in ref


def _local_path(ref: str) -> Path:
    if ref.startswith("file://"):
        return Path(url2pathname(urlsplit(ref).path))
    return Path(ref)


def load_catalog() -> dict[str, CatalogEntry]:
    """Load the discovery catalog from the configured socialware registry.

    ``HYPRIAL_INSTALL_REGISTRY`` names the **catalog document** (design §2): an
    ``https://`` URL, a ``file://`` URL, or a local path.  The document is a
    single JSON object; there is no clone and no per-entry git resolution.
    """

    registry = _registry_ref()
    if not registry:
        raise InstallError(
            "INSTALL_REGISTRY_INVALID",
            f"{_REGISTRY_ENV} must name a catalog document address",
        )
    is_local = _registry_is_local(registry)
    raw_text = _read_catalog_document(registry, is_local=is_local)
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise InstallError(
            "INSTALL_CATALOG_INVALID", f"cannot parse catalog {registry!r}: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise InstallError("INSTALL_CATALOG_INVALID", "installer catalog must be an object")
    if raw.get("schema") != _CATALOG_SCHEMA:
        raise InstallError(
            "INSTALL_CATALOG_INVALID",
            f"installer catalog schema must be {_CATALOG_SCHEMA!r}",
        )
    apps = raw.get("apps")
    if not isinstance(apps, dict):
        raise InstallError("INSTALL_CATALOG_INVALID", "installer catalog apps must be an object")
    catalog: dict[str, CatalogEntry] = {}
    for name, value in apps.items():
        catalog[name] = _parse_catalog_entry(name, value, registry_is_local=is_local)
    return catalog


def _read_catalog_document(registry: str, *, is_local: bool) -> str:
    if is_local:
        try:
            return _local_path(registry).read_text(encoding="utf-8")
        except OSError as error:
            raise InstallError(
                "INSTALL_REGISTRY_UNAVAILABLE",
                f"cannot read socialware catalog {registry!r}: {error}",
            ) from error
    # The catalog is the trust root: a network catalog must be https (owner audit
    # #4 / fable R1).  ``allow_loopback_http=False`` -- a loopback-http URL is a
    # *network* registry, not a local one, so it gets no plaintext concession.
    scheme_error = _artifact_url_error(registry, allow_local_file=False, allow_loopback_http=False)
    if scheme_error is not None:
        raise InstallError(
            "INSTALL_REGISTRY_INVALID",
            f"{_REGISTRY_ENV} names a catalog at {_redact_url(registry)!r} that {scheme_error}",
        )
    try:
        with _checked_urlopen(registry, allow_local=False) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise InstallError(
            "INSTALL_REGISTRY_UNAVAILABLE",
            f"cannot load socialware catalog {_redact_url(registry)!r}: {error}",
        ) from error


def _parse_catalog_entry(name: object, value: object, *, registry_is_local: bool) -> CatalogEntry:
    if not isinstance(name, str) or not isinstance(value, dict):
        raise InstallError("INSTALL_CATALOG_INVALID", "installer catalog entries must be objects")
    # release-only: a git/ref entry belongs to the developer path, not the
    # installer.  Loud, not silently ignored -- same principle as _V2_KEYS.
    if "git" in value or "ref" in value:
        raise InstallError(
            "INSTALL_CATALOG_INVALID",
            f"catalog entry {name!r} carries a git/ref source; git is a developer-only "
            f"path now ({_DEVELOPER_DOCS}), the catalog is release-only",
        )
    release = value.get("release")
    sha256 = value.get("sha256")
    version = value.get("version")
    commit = value.get("commit")
    manifest = value.get("manifest")
    if not all(isinstance(item, str) and item for item in (release, sha256, version, commit, manifest)):
        raise InstallError(
            "INSTALL_CATALOG_INVALID",
            f"catalog entry {name!r} requires release, sha256, version, commit, and manifest",
        )
    assert isinstance(sha256, str) and isinstance(commit, str)  # narrowed by the check above
    if not _SHA256.fullmatch(sha256.lower()):
        raise InstallError(
            "INSTALL_CATALOG_INVALID", f"catalog entry {name!r} sha256 must be 64 hex characters"
        )
    if not _COMMIT.fullmatch(commit.lower()):
        raise InstallError(
            "INSTALL_CATALOG_INVALID", f"catalog entry {name!r} commit must be a 40-hex git commit"
        )
    _parse_version(str(version), label=f"catalog entry {name!r} version", code="INSTALL_CATALOG_INVALID")
    _safe_relative_path(str(manifest), label=f"catalog entry {name!r} manifest")
    _reject_release_scheme(name, str(release), registry_is_local=registry_is_local)
    return CatalogEntry(
        name=name,
        release=str(release),
        sha256=sha256.lower(),
        version=str(version),
        commit=commit.lower(),
        manifest=str(manifest),
    )


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _redact_url(url: str) -> str:
    """Strip any ``user:pw@`` userinfo before a URL reaches an error, log, or
    receipt (owner audit r2).

    Userinfo is rejected outright by ``_artifact_url_error``, but the messages
    that echo the offending URL back must not carry the credential themselves.
    Rebuild the URL without userinfo, keeping host/port/path so the message still
    names the address at fault."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.username is None and parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _artifact_url_error(url: str, *, allow_local_file: bool, allow_loopback_http: bool) -> str | None:
    """Why a release URL is not acceptable, or ``None`` if it is.

    ``https`` always; ``file://`` only when ``allow_local_file`` (design §4
    boundary: a remote catalog may not point the installer at this machine's
    filesystem); ``http`` only to a loopback host **and only when the catalog
    itself is local** (``allow_loopback_http``) -- the sole plaintext concession,
    so the mandated local static-http fixture (design §9, and the 302→finalUrl
    path row 8 needs) can run, while a *remote* catalog can never carry the
    installer to plaintext -- not to a remote host and not even to this
    machine's loopback (owner audit D1: a remote catalog must not point at the
    local machine).  Production release URLs are ``https`` (P0 待裁项②).

    ⚠️ ``ftp`` and every other scheme fall through to the refusal below: CPython's
    redirect handler otherwise treats ``ftp`` as followable (fable R1)."""

    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        # Reject embedded userinfo outright (owner audit r2, low item promoted):
        # credentials in the URL leak two ways that are reachable in practice --
        # they get echoed into error text and receipts, and with HTTP(S)_PROXY
        # set the whole ``user:pw@host`` URL is handed to the proxy (NO_PROXY
        # matches on host only).  Never echo the userinfo back: report the host
        # only, so the message itself carries no credential.
        return (
            f"embeds userinfo credentials for host {parts.hostname!r}; "
            "credentials must not appear in the URL"
        )
    scheme = parts.scheme.lower()
    if scheme == "https":
        return None
    if scheme == "file":
        if allow_local_file:
            return None
        return (
            "points at a file:// artifact, but the catalog was loaded over the "
            "network; a remote catalog may not reference local files"
        )
    if scheme == "http":
        if allow_loopback_http and parts.hostname in _LOOPBACK_HOSTS:
            return None
        return (
            f"points at plaintext http host {parts.hostname!r}; http is allowed only to a "
            "loopback host and only when the catalog itself is local"
        )
    return f"release URL scheme {scheme!r} is not https or file"


def _reject_release_scheme(name: object, release: str, *, registry_is_local: bool) -> None:
    error = _artifact_url_error(
        release, allow_local_file=registry_is_local, allow_loopback_http=registry_is_local
    )
    if error is not None:
        raise InstallError("INSTALL_CATALOG_INVALID", f"catalog entry {name!r} {error}")


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run the scheme/host policy on **every** redirect hop (fable R1).

    CPython's default handler follows any ``http``/``https``/``ftp`` redirect
    with no host check, so a single hop off a trusted https catalog onto
    plaintext (or ftp, or a different host) would carry the whole fetch there
    unchecked -- and the catalog is the trust root, with no sha256 to fall back
    on.  Each hop is gated by the same ``_artifact_url_error`` as the initial
    URL; a refused hop raises rather than being followed."""

    def __init__(self, *, allow_local: bool) -> None:
        self._allow_local = allow_local

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        reason = _artifact_url_error(
            newurl, allow_local_file=self._allow_local, allow_loopback_http=self._allow_local
        )
        if reason is not None:
            raise urllib.error.HTTPError(
                newurl, code, f"refusing redirect to {_redact_url(newurl)!r}: {reason}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _checked_urlopen(url: str, *, allow_local: bool):
    """Open ``url`` with the scheme/host policy applied to the initial URL, every
    redirect hop, and the final URL (fable R1).  ``allow_local`` is true only when
    the catalog itself is local, and then (and only then) ``file://`` and
    loopback ``http`` are acceptable -- the same concession the catalog parser
    makes for a local catalog's release URLs.  Raises ``ValueError`` for a policy
    violation so the callers' existing ``ValueError`` handling maps it to their
    stable error code."""

    reason = _artifact_url_error(url, allow_local_file=allow_local, allow_loopback_http=allow_local)
    if reason is not None:
        raise ValueError(f"refusing to fetch {_redact_url(url)!r}: {reason}")
    opener = urllib.request.build_opener(_PolicyRedirectHandler(allow_local=allow_local))
    request = urllib.request.Request(url, headers={"User-Agent": "hyprial-installer"})
    response = opener.open(request, timeout=_HTTP_TIMEOUT)
    final_url = response.geturl()
    reason = _artifact_url_error(
        final_url, allow_local_file=allow_local, allow_loopback_http=allow_local
    )
    if reason is not None:
        response.close()
        raise ValueError(f"refusing redirected URL {_redact_url(final_url)!r}: {reason}")
    return response


def _safe_relative_path(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise InstallError("INSTALL_MANIFEST_INVALID", f"{label} must stay inside the repository")
    return path


def _parse_version(value: str, *, label: str, code: str = "INSTALL_STATE_INVALID") -> Version:
    try:
        return Version(value)
    except InvalidVersion as error:
        raise InstallError(code, f"{label} is not a valid version: {value!r}") from error


# --------------------------------------------------------------------------- #
# Release artifact: fetch (§5), verify sha256 (§5), safe extract (§1/§5)       #
# --------------------------------------------------------------------------- #


def _fetch_artifact(url: str, destination: Path, *, allow_local: bool) -> str:
    """GET the artifact to ``destination``; return the URL bytes actually came
    from (``finalUrl`` after any redirect).  Records the real path, not the
    catalog's claim -- the delivery-monitoring lesson (§3: a receipt that stores
    only the catalog address hides which route actually served the bytes).

    The initial URL, every redirect hop, and the final URL are all held to the
    same scheme/host policy (fable R1); ``allow_local`` is true only when the
    catalog itself is local, so a remote catalog can never redirect the fetch
    onto plaintext (or a local file)."""

    try:
        with _checked_urlopen(url, allow_local=allow_local) as response:
            final_url = response.geturl()
            with destination.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise InstallError(
            "INSTALL_ARTIFACT_UNAVAILABLE", f"cannot fetch artifact {_redact_url(url)!r}: {error}"
        ) from error
    return final_url


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify_artifact_digest(artifact: Path, expected: str, url: str) -> None:
    actual = _digest(artifact)
    if actual != expected:
        raise InstallError(
            "INSTALL_ARTIFACT_DIGEST_MISMATCH",
            f"artifact {url!r} sha256 {actual} does not match catalog pin {expected}",
            {"expected": expected, "actual": actual, "url": url},
        )


def _extract_source_tree(artifact: Path, destination: Path, *, name: str, version: str) -> Path:
    """Extract the ``<name>-<version>/`` source tree, rejecting every unsafe
    member *before* writing anything to disk (design §1/§5)."""

    prefix = f"{name}-{version}/"
    prefix_dir = prefix.rstrip("/")
    try:
        with tarfile.open(artifact, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                _reject_unsafe_member(member, prefix=prefix, prefix_dir=prefix_dir)
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            tar.extractall(destination, filter="data")
    except tarfile.TarError as error:
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED", f"cannot read release artifact: {error}"
        ) from error
    root = destination / prefix_dir
    if not root.is_dir():
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED",
            f"release artifact has no single top-level {prefix!r} directory",
        )
    return root


def _reject_unsafe_member(member: tarfile.TarInfo, *, prefix: str, prefix_dir: str) -> None:
    if member.issym() or member.islnk():
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED",
            f"release artifact member {member.name!r} is a symlink or hardlink",
        )
    if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED",
            f"release artifact member {member.name!r} is a device or special file",
        )
    posix = PurePosixPath(member.name)
    if posix.is_absolute() or any(part == ".." for part in posix.parts):
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED",
            f"release artifact member {member.name!r} escapes the archive root",
        )
    if member.name != prefix_dir and not member.name.startswith(prefix):
        raise InstallError(
            "INSTALL_ARTIFACT_MALFORMED",
            f"release artifact member {member.name!r} is outside the single {prefix!r} prefix",
        )


# --------------------------------------------------------------------------- #
# Source manifest & dirty judgment (§5), replacing git status for v2           #
# --------------------------------------------------------------------------- #


def _build_source_manifest(source: Path) -> dict[str, Any]:
    """Snapshot the extracted tree *before* install.sh runs (design §5)."""

    files: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            # Extraction already rejected link members; a link here means the
            # tree was tampered with after extraction -- refuse, do not record.
            raise InstallError(
                "INSTALL_ARTIFACT_MALFORMED",
                f"source tree contains a symlink at {path.relative_to(source).as_posix()!r}",
            )
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": _digest(path),
                "exec": bool(path.stat().st_mode & 0o111),
            }
        )
    files.sort(key=lambda entry: entry["path"])
    return {"schema": _SOURCE_MANIFEST_SCHEMA, "files": files}


def _read_source_manifest(app_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    reference = receipt.get("sourceManifest")
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise InstallError(
            "INSTALL_STATE_INVALID", "v2 receipt sourceManifest must record a path and sha256"
        )
    _safe_relative_path(reference["path"], label="receipt sourceManifest path")
    manifest_path = app_root.joinpath(*PurePosixPath(reference["path"]).parts)
    expected_sha = reference.get("sha256")
    if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha.lower()) is None:
        raise InstallError(
            "INSTALL_STATE_INVALID", "v2 receipt sourceManifest sha256 must be 64 hex characters"
        )
    if not manifest_path.is_file() or _digest(manifest_path) != expected_sha.lower():
        # The manifest is the integrity anchor: if it is missing or altered the
        # tree cannot be judged, so treat it as dirty (loud) rather than clean.
        raise InstallError(
            "INSTALL_SOURCE_DIRTY",
            "the recorded source manifest is missing or altered; refusing to trust the tree",
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError(
            "INSTALL_STATE_INVALID", f"cannot read source manifest: {error}"
        ) from error
    if not isinstance(raw, dict) or raw.get("schema") != _SOURCE_MANIFEST_SCHEMA:
        raise InstallError("INSTALL_STATE_INVALID", "source manifest schema is not recognised")
    return raw


def _source_is_dirty(source: Path, manifest: dict[str, Any]) -> bool:
    """dirty ⇔ any manifest file is missing, changed, or has a flipped exec bit.

    Files *not* in the manifest are ignored -- exactly ``git status
    --untracked-files=no`` (design §5)."""

    for entry in manifest.get("files", []):
        relative = entry["path"]
        path = source.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            return True
        if _digest(path) != entry["sha256"]:
            return True
        if bool(path.stat().st_mode & 0o111) != bool(entry.get("exec", False)):
            return True
    return False


def _v2_source_dirty(app_root: Path, source: Path, receipt: dict[str, Any]) -> bool:
    manifest = _read_source_manifest(app_root, receipt)
    return _source_is_dirty(source, manifest)


# --------------------------------------------------------------------------- #
# Git face kept for v1 launch only (§4 P2a "验存量"); P2b deletes it           #
# --------------------------------------------------------------------------- #


def _run_git(args: list[str], *, cwd: Path | None = None, timeout: float = _GIT_TIMEOUT) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise InstallError(
            "INSTALL_GIT_FAILED",
            f"cannot run git {' '.join(args)}: {error}\n{_GIT_TIMEOUT_HINT}",
        ) from error
    except OSError as error:
        # No hint here: a missing git binary is not a slow tunnel, and the
        # non-zero-exit path below must stay byte-identical to what shipped.
        raise InstallError("INSTALL_GIT_FAILED", f"cannot run git {' '.join(args)}: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git failed"
        if _is_git_auth_failure(detail):
            detail = f"{detail}\n{_GIT_AUTH_HINT}"
        raise InstallError("INSTALL_GIT_FAILED", detail)
    return completed.stdout.strip()


def _is_git_auth_failure(detail: str) -> bool:
    """Whether git's own error text says the read was refused, not merely slow."""

    lowered = detail.lower()
    return any(marker in lowered for marker in _GIT_AUTH_FAILURE_MARKERS)


def _existing_git_source(name: str, git_url: str, commit: str, source: Path) -> bool:
    """Validate an already-installed v1 (git) checkout for launch (§4 P2a).

    ⛔ Reachable only from the v1 launch path; new installs/upgrades never
    produce a git checkout.  Behaviour is unchanged from the pre-release code so
    v1 ``start`` stays byte-identical (design §3 P2a, test row 19)."""

    if not source.exists():
        return False
    if not (source / ".git").is_dir():
        raise InstallError("INSTALL_SOURCE_CONFLICT", f"{source} is not a hyprial-managed Git checkout")
    remote = _run_git(["config", "--get", "remote.origin.url"], cwd=source)
    actual = _run_git(["rev-parse", "HEAD"], cwd=source).lower()
    if remote != git_url:
        raise InstallError(
            "INSTALL_SOURCE_CONFLICT",
            f"{source} belongs to {remote!r}, not {git_url!r}",
        )
    if actual != commit:
        raise InstallError(
            "INSTALL_UPDATE_UNSUPPORTED",
            f"{name} is at {actual}; MVP install will not move it to {commit}",
            {"installedCommit": actual, "resolvedCommit": commit},
        )
    return True


# --------------------------------------------------------------------------- #
# Manifest (§1: unchanged tree contract, verbatim from the git era)           #
# --------------------------------------------------------------------------- #


def _read_manifest(source: Path, *, name: str, manifest_rel: str) -> Manifest:
    relative = _safe_relative_path(manifest_rel, label="manifest path")
    path = source.joinpath(*relative.parts)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InstallError("INSTALL_MANIFEST_INVALID", f"missing {manifest_rel}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError("INSTALL_MANIFEST_INVALID", f"cannot read {manifest_rel}: {error}") from error
    if not isinstance(raw, dict):
        raise InstallError("INSTALL_MANIFEST_INVALID", "install manifest must be an object")
    schema = raw.get("schema")
    if schema not in _SCHEMAS:
        raise InstallError(
            "INSTALL_MANIFEST_INVALID",
            f"install manifest schema must be one of {list(_SCHEMAS)!r}",
        )
    if schema == _SCHEMA_V2:
        unknown = sorted(set(raw) - _V2_KEYS)
        if unknown:
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                f"{_SCHEMA_V2} does not accept keys {unknown!r}",
            )
    if raw.get("name") != name:
        raise InstallError(
            "INSTALL_MANIFEST_INVALID",
            f"install manifest name must be {name!r}",
        )
    install = raw.get("install")
    if install != list(_INSTALL_ARGV):
        raise InstallError(
            "INSTALL_MANIFEST_INVALID",
            f"{_SCHEMA} install must be {list(_INSTALL_ARGV)!r}",
        )
    script = source / _INSTALL_ARGV[1]
    if not script.is_file() or script.is_symlink():
        raise InstallError(
            "INSTALL_MANIFEST_INVALID",
            "install.sh must be a regular file at the repository root",
        )
    start_raw = raw.get("start")
    start: tuple[str, ...] | None = None
    if start_raw is not None:
        if (
            not isinstance(start_raw, list)
            or len(start_raw) != 2
            or start_raw[0] != "bash"
            or not isinstance(start_raw[1], str)
        ):
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                f"{_SCHEMA} start must be ['bash', '<repository script>']",
            )
        start_path = _safe_relative_path(start_raw[1], label="start script")
        start_script = source.joinpath(*start_path.parts)
        if not start_script.is_file() or start_script.is_symlink():
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                "start script must be a regular file inside the repository",
            )
        start = ("bash", start_raw[1])
    commands: tuple[MountedCommand, ...] = ()
    if schema == _SCHEMA_V2:
        commands = _parse_commands(raw.get("commands"), app_name=name)
        _reject_subscriptions(raw.get("subscriptions"))
    return Manifest(name=name, install=_INSTALL_ARGV, start=start, commands=commands)


def _parse_commands(raw: Any, *, app_name: str) -> tuple[MountedCommand, ...]:
    """Parse ``commands`` (H3).  Absent is fine; present-and-wrong is not."""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InstallError("INSTALL_MANIFEST_INVALID", "manifest commands must be a list")
    seen: set[str] = set()
    parsed: list[MountedCommand] = []
    for index, item in enumerate(raw):
        label = f"commands[{index}]"
        if not isinstance(item, dict):
            raise InstallError("INSTALL_MANIFEST_INVALID", f"{label} must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not _COMMAND_NAME.match(name):
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                f"{label}.name must match {_COMMAND_NAME.pattern!r}; got {name!r}",
            )
        if name in seen:
            raise InstallError("INSTALL_MANIFEST_INVALID", f"{label}.name {name!r} is declared twice")
        seen.add(name)
        summary = item.get("summary", "")
        if not isinstance(summary, str):
            raise InstallError("INSTALL_MANIFEST_INVALID", f"{label}.summary must be a string")
        actions_raw = item.get("actions", ["start"])
        if (
            not isinstance(actions_raw, list)
            or not actions_raw
            or not all(isinstance(action, str) for action in actions_raw)
        ):
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                f"{label}.actions must be a non-empty list of strings",
            )
        unknown_actions = sorted(set(actions_raw) - _COMMAND_ACTIONS)
        if unknown_actions:
            raise InstallError(
                "INSTALL_MANIFEST_INVALID",
                f"{label}.actions {unknown_actions!r} not in {sorted(_COMMAND_ACTIONS)!r}",
            )
        parsed.append(
            MountedCommand(name=name, summary=summary, actions=frozenset(actions_raw))
        )
    return tuple(parsed)


def _reject_subscriptions(raw: Any) -> None:
    """``subscriptions`` is the H2 slot: it may appear, but must be empty."""

    if raw is None:
        return
    if not isinstance(raw, list):
        raise InstallError("INSTALL_MANIFEST_INVALID", "manifest subscriptions must be a list")
    if raw:
        raise InstallError(
            "INSTALL_MANIFEST_INVALID",
            f"manifest subscriptions names {len(raw)} entries but this release ships no "
            "event subscriptions -- this is the H2 slot (design-app-manifest-commands §1)",
        )


# --------------------------------------------------------------------------- #
# install.sh execution (§5 env injection added)                               #
# --------------------------------------------------------------------------- #


def _run_install(
    manifest: Manifest,
    *,
    source: Path,
    hyprial_home: Path,
    json_output: bool,
    plan: dict[str, Any],
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HYPRIAL_HOME"] = str(hyprial_home)
    if env_extra:
        env.update(env_extra)
    try:
        completed = subprocess.run(
            list(manifest.install),
            cwd=source,
            env=env,
            text=True,
            capture_output=json_output,
            check=False,
            timeout=_INSTALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise InstallError("INSTALL_SCRIPT_FAILED", f"cannot run install.sh: {error}", plan) from error
    if completed.returncode != 0:
        failure = {**plan, "exitCode": completed.returncode}
        if json_output:
            failure.update(stdout=completed.stdout, stderr=completed.stderr)
        raise InstallError(
            "INSTALL_SCRIPT_FAILED",
            f"{manifest.name} install.sh exited with {completed.returncode}",
            failure,
        )
    return completed


def _source_env(entry_or_source: dict[str, Any]) -> dict[str, str]:
    """The two source-provenance variables install.sh / start may read (§5)."""

    return {
        "HYPRIAL_SOURCE_COMMIT": str(entry_or_source["commit"]),
        "HYPRIAL_SOURCE_VERSION": str(entry_or_source["version"]),
    }


def _write_source_manifest(app_root: Path, manifest_doc: dict[str, Any]) -> str:
    path = app_root / _SOURCE_MANIFEST_FILE
    atomic_json_write(path, manifest_doc)
    return _digest(path)


# --------------------------------------------------------------------------- #
# install (§1/§5): fetch → verify → safe-extract → confirm → install.sh        #
# --------------------------------------------------------------------------- #


def install_application(
    name: str,
    *,
    hyprial_home: Path,
    confirm: Callable[[dict[str, Any]], bool],
    json_output: bool = False,
) -> dict[str, Any]:
    """Install one catalog application from its pinned release artifact."""

    catalog = load_catalog()
    entry = catalog.get(name)
    if entry is None:
        available = sorted(catalog)
        raise InstallError(
            "INSTALLER_NOT_FOUND",
            f"unknown installer {name!r}; available: {', '.join(available) or 'none'}",
            {"available": available},
        )

    registry = _registry_ref()
    allow_local = _registry_is_local(registry)
    app_root = hyprial_home / "apps" / name
    source = app_root / "source"
    if source.exists():
        raise InstallError(
            "INSTALL_SOURCE_CONFLICT",
            f"{name} already has a source tree at {source}; upgrade it instead",
        )
    app_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=app_root))
    try:
        tree, final_url = _stage_release(entry, staging, allow_local=allow_local)
        manifest = _read_manifest(tree, name=name, manifest_rel=entry.manifest)
        source_doc = _build_source_manifest(tree)
        plan: dict[str, Any] = {
            "ok": True,
            "name": name,
            "registry": registry,
            "source": {
                "type": "release",
                "url": entry.release,
                "finalUrl": final_url,
                "sha256": entry.sha256,
                "version": entry.version,
                "commit": entry.commit,
            },
            "install": list(manifest.install),
        }
        if not confirm(plan):
            raise InstallError("INSTALL_DECLINED", f"installation of {name!r} was declined", plan)

        os.replace(tree, source)
        try:
            completed = _run_install(
                manifest,
                source=source,
                hyprial_home=hyprial_home,
                json_output=json_output,
                plan=plan,
                env_extra=_source_env({"commit": entry.commit, "version": entry.version}),
            )
            # Persisted only after install.sh succeeds, but its *content* is the
            # pre-install.sh snapshot (built from the staged tree above) -- so a
            # failed install leaves no orphan manifest, and install.sh cannot wash
            # a change into the recorded baseline (design §5).
            manifest_sha = _write_source_manifest(app_root, source_doc)
            receipt = _release_receipt(
                name,
                entry,
                final_url=final_url,
                registry=registry,
                manifest_sha=manifest_sha,
                installed_at=datetime.now(UTC).isoformat(),
            )
            atomic_json_write(app_root / "install.json", receipt)
        except BaseException:
            # ⚠️ Roll back a *first* install that failed after the tree was moved
            # into place (owner audit B1): without this the app keeps a source/
            # tree and no receipt, so the next `hyprial install` reports
            # SOURCE_CONFLICT and `upgrade --force` reports NOT_INSTALLED -- the
            # app becomes permanently un-installable after one flaky install.sh.
            # Removing source/ (and any half-written manifest) restores the
            # not-installed state, so a retry can succeed.
            shutil.rmtree(source, ignore_errors=True)
            (app_root / _SOURCE_MANIFEST_FILE).unlink(missing_ok=True)
            raise
        result = {"ok": True, "installed": True, **receipt}
        if json_output:
            result.update(stdout=completed.stdout, stderr=completed.stderr)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _stage_release(entry: CatalogEntry, staging: Path, *, allow_local: bool) -> tuple[Path, str]:
    """Fetch, verify, and safe-extract into ``staging``; return (tree, finalUrl).

    The digest is checked **before** extraction, so a mismatch leaves nothing
    but the staged tarball -- which the caller's ``finally`` removes (§9.2)."""

    artifact = staging / "artifact.tar.gz"
    final_url = _fetch_artifact(entry.release, artifact, allow_local=allow_local)
    _verify_artifact_digest(artifact, entry.sha256, entry.release)
    tree = _extract_source_tree(artifact, staging / "tree", name=entry.name, version=entry.version)
    return tree, final_url


def _release_receipt(
    name: str,
    entry: CatalogEntry,
    *,
    final_url: str,
    registry: str,
    manifest_sha: str,
    installed_at: str,
    upgraded_at: str | None = None,
    previous_version: str | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema": _RECEIPT_SCHEMA_V2,
        "name": name,
        "source": {
            "type": "release",
            "url": entry.release,
            "finalUrl": final_url,
            "sha256": entry.sha256,
            "version": entry.version,
            "commit": entry.commit,
        },
        "registry": registry,
        "manifest": entry.manifest,
        "sourceManifest": {"path": _SOURCE_MANIFEST_FILE, "sha256": manifest_sha},
        "installedAt": installed_at,
    }
    if upgraded_at is not None:
        receipt["upgradedAt"] = upgraded_at
    if previous_version is not None:
        receipt["previousVersion"] = previous_version
    return receipt


# --------------------------------------------------------------------------- #
# upgrade (§5 matrix) and v1→release migration (§6)                            #
# --------------------------------------------------------------------------- #


def upgrade_application(
    name: str,
    *,
    hyprial_home: Path,
    confirm: Callable[[dict[str, Any]], bool],
    check_only: bool = False,
    force: bool = False,
    json_output: bool = False,
    before_apply: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Check or atomically upgrade one installed catalog application.

    A v1 (git) receipt routes into migration (§6); a v2 receipt takes the
    release relation matrix (§5)."""

    catalog = load_catalog()
    entry = catalog.get(name)
    if entry is None:
        available = sorted(catalog)
        raise InstallError(
            "INSTALLER_NOT_FOUND",
            f"unknown installer {name!r}; available: {', '.join(available) or 'none'}",
            {"available": available},
        )

    app_root = hyprial_home / "apps" / name
    receipt = _read_install_receipt(app_root, name)
    if receipt["schema"] == _RECEIPT_SCHEMA_V1:
        return _migrate_v1_receipt(
            name,
            entry,
            receipt,
            hyprial_home=hyprial_home,
            confirm=confirm,
            check_only=check_only,
            force=force,
            json_output=json_output,
            before_apply=before_apply,
        )
    return _upgrade_v2(
        name,
        entry,
        receipt,
        hyprial_home=hyprial_home,
        confirm=confirm,
        check_only=check_only,
        force=force,
        json_output=json_output,
        before_apply=before_apply,
    )


def _release_relation(entry: CatalogEntry, source: dict[str, Any]) -> str:
    catalog_version = _parse_version(entry.version, label="catalog version", code="INSTALL_CATALOG_INVALID")
    installed_version = _parse_version(str(source["version"]), label="receipt version")
    if catalog_version == installed_version:
        return "current" if entry.sha256 == str(source["sha256"]).lower() else "artifact-changed"
    return "upgrade" if catalog_version > installed_version else "downgrade"


def _upgrade_v2(
    name: str,
    entry: CatalogEntry,
    receipt: dict[str, Any],
    *,
    hyprial_home: Path,
    confirm: Callable[[dict[str, Any]], bool],
    check_only: bool,
    force: bool,
    json_output: bool,
    before_apply: Callable[[], None] | None,
) -> dict[str, Any]:
    app_root = hyprial_home / "apps" / name
    source = app_root / "source"
    registry = _registry_ref()
    installed = receipt["source"]
    relation = _release_relation(entry, installed)
    # ``force`` (and a read-only ``--check``) must still reach a verdict when the
    # source manifest is missing or altered (owner audit #5): ``_v2_source_dirty``
    # raises INSTALL_SOURCE_DIRTY in that case, which would otherwise fire *before*
    # the force gate below and leave a broken install un-rescuable.  Treat an
    # untrustworthy manifest as dirty so ``--force`` can replace the whole tree.
    try:
        dirty = _v2_source_dirty(app_root, source, receipt)
    except InstallError as error:
        if error.code == "INSTALL_SOURCE_DIRTY" and (force or check_only):
            dirty = True
        else:
            raise
    plan: dict[str, Any] = {
        "ok": True,
        "name": name,
        "registry": registry,
        "source": {
            "type": "release",
            "url": entry.release,
            "sha256": entry.sha256,
            "version": entry.version,
            "commit": entry.commit,
        },
        "installed": {
            "version": installed["version"],
            "sha256": str(installed["sha256"]).lower(),
            "commit": str(installed["commit"]).lower(),
        },
        "relation": relation,
        "sourceDirty": dirty,
        "updateAvailable": relation == "upgrade",
        "forced": force,
    }
    if check_only:
        return {**plan, "checked": True, "upgraded": False}
    if relation == "artifact-changed":
        # ⛔ Not forceable: the publisher broke immutability; the fix is a new
        # version, not a local overwrite (design §5, test row 10).
        raise InstallError(
            "INSTALL_ARTIFACT_CHANGED",
            f"{name} catalog version {entry.version} kept its number but changed sha256; "
            "publish a new version instead of reinstalling",
            plan,
        )
    if relation == "current" and not force:
        return {**plan, "checked": True, "upgraded": False, "alreadyCurrent": True}
    if dirty and not force:
        raise InstallError(
            "INSTALL_SOURCE_DIRTY",
            f"the installed {name} source has local changes; rerun with --force to replace it",
            plan,
        )
    if relation == "downgrade" and not force:
        raise InstallError(
            "INSTALL_CATALOG_BEHIND",
            f"{name} catalog version {entry.version} is older than installed {installed['version']}",
            plan,
        )
    return _apply_release_over_existing(
        name,
        entry,
        receipt,
        plan,
        hyprial_home=hyprial_home,
        confirm=confirm,
        json_output=json_output,
        before_apply=before_apply,
        previous_version=str(installed["version"]),
        force=force,
    )


def _migrate_v1_receipt(
    name: str,
    entry: CatalogEntry,
    receipt: dict[str, Any],
    *,
    hyprial_home: Path,
    confirm: Callable[[dict[str, Any]], bool],
    check_only: bool,
    force: bool,
    json_output: bool,
    before_apply: Callable[[], None] | None,
) -> dict[str, Any]:
    """Migrate an installed git (v1) app to the release path (design §6).

    One fresh release install into staged + the existing swap mechanism; no git
    dirty check (the git face is gone).  The old tree, ``.git`` and all, leaves
    with the backup."""

    registry = _registry_ref()
    from_commit = str(receipt["sourceCommit"]).lower()
    plan: dict[str, Any] = {
        "ok": True,
        "name": name,
        "registry": registry,
        "migration": True,
        "relation": "migrate",
        # design §6 / fable R5: migration does NOT run a git dirty check (the git
        # face is gone), so this warning is the only compensating control -- the
        # operator confirming a migration over a locally-edited v1 tree must be
        # told those edits are discarded when source/ is replaced wholesale.
        "warning": (
            "迁移到 release 会用发布产物整体替换 source/;"
            "本地对源码树的改动将随迁移一并丢弃(migration does not preserve local changes)。"
        ),
        "from": {"type": "git", "commit": from_commit},
        "to": {
            "type": "release",
            "url": entry.release,
            "sha256": entry.sha256,
            "version": entry.version,
            "commit": entry.commit,
        },
    }
    if check_only:
        return {**plan, "checked": True, "upgraded": False}
    return _apply_release_over_existing(
        name,
        entry,
        receipt,
        plan,
        hyprial_home=hyprial_home,
        confirm=confirm,
        json_output=json_output,
        before_apply=before_apply,
        previous_version=None,
        migrated=True,
        force=force,
    )


def _apply_release_over_existing(
    name: str,
    entry: CatalogEntry,
    receipt: dict[str, Any],
    plan: dict[str, Any],
    *,
    hyprial_home: Path,
    confirm: Callable[[dict[str, Any]], bool],
    json_output: bool,
    before_apply: Callable[[], None] | None,
    previous_version: str | None,
    migrated: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Staged fetch → backup → swap → install.sh → v2 receipt, with rollback.

    Shared by the v2 upgrade and the v1 migration; both replace an existing
    ``source/`` atomically and reuse the proven backup/rollback mechanism.  When
    the recorded ``source/`` is gone, ``force`` reinstalls a fresh tree in place
    instead of dead-looping (owner audit r2 #7 / charter 5e)."""

    app_root = hyprial_home / "apps" / name
    source = app_root / "source"
    manifest_path = app_root / _SOURCE_MANIFEST_FILE
    source_present = source.exists()
    # With `_installed_source` gone, a migration or forced upgrade over a receipt
    # whose source/ tree is missing used to fall through to a bare
    # FileNotFoundError from the swap below.  Name the error up front (owner audit
    # D2/#7) *and* make it walkable (owner audit r2 #7 / charter 5e): plain
    # `hyprial install`/upgrade both route back here and hit the same error, so
    # --force -- which reinstalls a fresh tree below -- is the one way out, and the
    # message must name only that.
    if not source_present and not force:
        raise InstallError(
            "INSTALL_SOURCE_MISSING",
            f"the recorded {name} source is missing at {source}; "
            "rerun with --force to reinstall it",
        )
    allow_local = _registry_is_local(_registry_ref())
    staging = Path(tempfile.mkdtemp(prefix=".stage-upgrade-", dir=app_root))
    backup = app_root / f".source-backup-{uuid4().hex}"
    manifest_backup: Path | None = None
    source_swapped = False
    try:
        tree, final_url = _stage_release(entry, staging, allow_local=allow_local)
        manifest = _read_manifest(tree, name=name, manifest_rel=entry.manifest)
        source_doc = _build_source_manifest(tree)
        plan["install"] = list(manifest.install)
        if not confirm(plan):
            raise InstallError("INSTALL_DECLINED", f"upgrade of {name!r} was declined", plan)
        if before_apply is not None:
            before_apply()

        if source_present:
            os.replace(source, backup)
        os.replace(tree, source)
        source_swapped = True
        try:
            completed = _run_install(
                manifest,
                source=source,
                hyprial_home=hyprial_home,
                json_output=json_output,
                plan=plan,
                env_extra=_source_env({"commit": entry.commit, "version": entry.version}),
            )
        except BaseException:
            failed = app_root / f".source-failed-{uuid4().hex}"
            os.replace(source, failed)
            if source_present:
                os.replace(backup, source)
            source_swapped = False
            shutil.rmtree(failed, ignore_errors=True)
            raise

        # Manifest + receipt are the atomic commit, written only once install.sh
        # has succeeded, and they must flip old→new *together*: source/ is already
        # the new tree (swapped above), so if the receipt write fails after the
        # manifest write, the rolled-back old tree would be paired with a *new*
        # manifest and launch would loudly report INSTALL_SOURCE_DIRTY (owner
        # audit r2 #9).  Back the old manifest up so the rollback restores the
        # matching old {tree, manifest, receipt} set.
        if manifest_path.exists():
            manifest_backup = app_root / f".source-manifest-{uuid4().hex}.bak"
            os.replace(manifest_path, manifest_backup)
        manifest_sha = _write_source_manifest(app_root, source_doc)
        upgraded_at = datetime.now(UTC).isoformat()
        next_receipt = _release_receipt(
            name,
            entry,
            final_url=final_url,
            registry=_registry_ref(),
            manifest_sha=manifest_sha,
            installed_at=str(receipt.get("installedAt", upgraded_at)),
            upgraded_at=upgraded_at,
            previous_version=previous_version,
        )
        atomic_json_write(app_root / "install.json", next_receipt)
        # Committed: new source/, manifest, and receipt are all in place.
        if manifest_backup is not None:
            manifest_backup.unlink(missing_ok=True)
            manifest_backup = None
        if source_present:
            shutil.rmtree(backup, ignore_errors=True)
        source_swapped = False
        result = {**plan, "checked": True, "upgraded": True, **next_receipt}
        if migrated:
            result["migrated"] = True
        if json_output:
            result.update(stdout=completed.stdout, stderr=completed.stderr)
        return result
    finally:
        if source_swapped:
            # install.sh succeeded but the commit (manifest/receipt) failed: undo
            # the swap and restore the old {tree, manifest} so the old receipt --
            # never overwritten -- stays launchable (owner audit r2 #9).
            failed = app_root / f".source-failed-{uuid4().hex}"
            if source.exists():
                os.replace(source, failed)
                shutil.rmtree(failed, ignore_errors=True)
            if source_present and backup.exists():
                os.replace(backup, source)
            if manifest_backup is not None and manifest_backup.exists():
                os.replace(manifest_backup, manifest_path)
            elif not source_present:
                # forced reinstall over a missing source/: no old manifest to
                # pair, so drop any half-written new one -- back to receipt-only.
                manifest_path.unlink(missing_ok=True)
        elif source_present and backup.exists() and not source.exists():
            # fable R8: the backup was taken but the second rename (tree→source)
            # never landed, so source/ is gone while the backup is orphaned.
            # Put the original tree back rather than leave the app with no source/.
            os.replace(backup, source)
        if manifest_backup is not None:
            manifest_backup.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Receipt reading (v1 + v2 coexist, §3)                                        #
# --------------------------------------------------------------------------- #


def _read_install_receipt(app_root: Path, name: str) -> dict[str, Any]:
    path = app_root / "install.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InstallError(
            "APPLICATION_NOT_INSTALLED",
            f"{name} is not installed; run: hyprial install {name}",
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError(
            "INSTALL_STATE_INVALID", f"cannot read {name} install receipt: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise InstallError("INSTALL_STATE_INVALID", "install receipt must be an object")
    if raw.get("name") != name:
        raise InstallError("INSTALL_STATE_INVALID", f"install receipt name must be {name!r}")
    schema = raw.get("schema")
    if schema == _RECEIPT_SCHEMA_V1:
        _validate_v1_receipt(raw)
    elif schema == _RECEIPT_SCHEMA_V2:
        _validate_v2_receipt(raw)
    else:
        raise InstallError(
            "INSTALL_STATE_INVALID",
            f"install receipt schema must be {_RECEIPT_SCHEMA_V1!r} or {_RECEIPT_SCHEMA_V2!r}",
        )
    return raw


def _validate_v1_receipt(raw: dict[str, Any]) -> None:
    commit = raw.get("sourceCommit")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit.lower()) is None:
        raise InstallError(
            "INSTALL_STATE_INVALID", "install receipt sourceCommit must be a Git commit"
        )
    for field in ("sourceUrl", "sourceRef"):
        if not isinstance(raw.get(field), str) or not raw[field]:
            raise InstallError(
                "INSTALL_STATE_INVALID",
                f"install receipt {field} must be a non-empty string",
            )
    manifest = raw.get("manifest", "hyprial-install.json")
    if not isinstance(manifest, str) or not manifest:
        raise InstallError(
            "INSTALL_STATE_INVALID",
            "install receipt manifest must be a non-empty string",
        )
    _safe_relative_path(manifest, label="install receipt manifest")
    raw["manifest"] = manifest


def _validate_v2_receipt(raw: dict[str, Any]) -> None:
    source = raw.get("source")
    if not isinstance(source, dict):
        raise InstallError("INSTALL_STATE_INVALID", "v2 install receipt source must be an object")
    if source.get("type") != "release":
        raise InstallError("INSTALL_STATE_INVALID", "v2 install receipt source type must be 'release'")
    url = source.get("url")
    # The receipt is local trusted state, so a file:// url (dev install) or a
    # loopback-http url (the local static-http fixture) written at install time
    # is fine on read.
    if not isinstance(url, str) or _artifact_url_error(
        url, allow_local_file=True, allow_loopback_http=True
    ) is not None:
        raise InstallError(
            "INSTALL_STATE_INVALID", "v2 install receipt source url must be an https or file URL"
        )
    sha256 = source.get("sha256")
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256.lower()) is None:
        raise InstallError(
            "INSTALL_STATE_INVALID", "v2 install receipt source sha256 must be 64 hex characters"
        )
    commit = source.get("commit")
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit.lower()) is None:
        raise InstallError(
            "INSTALL_STATE_INVALID", "v2 install receipt source commit must be a Git commit"
        )
    _parse_version(str(source.get("version")), label="v2 receipt source version")
    manifest = raw.get("manifest", "hyprial-install.json")
    if not isinstance(manifest, str) or not manifest:
        raise InstallError(
            "INSTALL_STATE_INVALID", "install receipt manifest must be a non-empty string"
        )
    _safe_relative_path(manifest, label="install receipt manifest")
    raw["manifest"] = manifest


# --------------------------------------------------------------------------- #
# launch (§3/§5): v1 keeps the git checks byte-identical; v2 uses the manifest  #
# --------------------------------------------------------------------------- #


def _exec_application(argv: tuple[str, ...], cwd: Path, env: dict[str, str]) -> Any:
    os.chdir(cwd)
    os.execvpe(argv[0], list(argv), env)


def prepare_application_launch(
    name: str,
    *,
    hyprial_home: Path,
) -> ApplicationLaunch:
    """Validate and describe an installed application's exact launch.

    A v1 (git) receipt keeps the pre-release validation verbatim so existing
    nodes start unchanged (§3 P2a, row 19); a v2 receipt validates against the
    source manifest and injects ``HYPRIAL_SOURCE_COMMIT/VERSION`` (§5)."""

    app_root = hyprial_home / "apps" / name
    receipt = _read_install_receipt(app_root, name)
    source = app_root / "source"
    if receipt["schema"] == _RECEIPT_SCHEMA_V1:
        return _prepare_v1_launch(name, receipt, source, hyprial_home)
    return _prepare_v2_launch(name, receipt, app_root, source, hyprial_home)


def _prepare_v1_launch(
    name: str, receipt: dict[str, Any], source: Path, hyprial_home: Path
) -> ApplicationLaunch:
    git_url = str(receipt["sourceUrl"])
    commit = str(receipt["sourceCommit"]).lower()
    manifest_rel = str(receipt["manifest"])
    if not _existing_git_source(name, git_url, commit, source):
        raise InstallError(
            "INSTALL_SOURCE_MISSING",
            f"the recorded {name} source is missing; "
            f"reinstall it: hyprial install {name} --force",
        )
    if _run_git(["status", "--porcelain", "--untracked-files=no"], cwd=source):
        raise InstallError(
            "INSTALL_SOURCE_DIRTY",
            f"the installed {name} source has tracked changes; refusing to launch it",
        )
    manifest = _read_manifest(source, name=name, manifest_rel=manifest_rel)
    if manifest.start is None:
        raise InstallError(
            "APPLICATION_START_UNAVAILABLE",
            f"{name} does not declare a start command",
        )
    env = dict(os.environ)
    env["HYPRIAL_HOME"] = str(hyprial_home)
    return ApplicationLaunch(
        name=name,
        argv=manifest.start,
        cwd=source,
        env=env,
        source_commit=commit,
    )


def _prepare_v2_launch(
    name: str, receipt: dict[str, Any], app_root: Path, source: Path, hyprial_home: Path
) -> ApplicationLaunch:
    installed = receipt["source"]
    commit = str(installed["commit"]).lower()
    version = str(installed["version"])
    manifest_rel = str(receipt["manifest"])
    if not source.is_dir():
        raise InstallError(
            "INSTALL_SOURCE_MISSING",
            f"the recorded {name} source is missing; "
            f"reinstall it: hyprial install {name} --force",
        )
    if _v2_source_dirty(app_root, source, receipt):
        raise InstallError(
            "INSTALL_SOURCE_DIRTY",
            f"the installed {name} source has local changes; refusing to launch it",
        )
    manifest = _read_manifest(source, name=name, manifest_rel=manifest_rel)
    if manifest.start is None:
        raise InstallError(
            "APPLICATION_START_UNAVAILABLE",
            f"{name} does not declare a start command",
        )
    env = dict(os.environ)
    env["HYPRIAL_HOME"] = str(hyprial_home)
    env["HYPRIAL_SOURCE_COMMIT"] = commit
    env["HYPRIAL_SOURCE_VERSION"] = version
    return ApplicationLaunch(
        name=name,
        argv=manifest.start,
        cwd=source,
        env=env,
        source_commit=commit,
    )


def launch_application(
    name: str,
    *,
    hyprial_home: Path,
    execute: Callable[[tuple[str, ...], Path, dict[str, str]], Any] = _exec_application,
) -> Any:
    """Launch an installed application from its recorded, exact checkout."""

    launch = prepare_application_launch(name, hyprial_home=hyprial_home)
    return execute(launch.argv, launch.cwd, launch.env)


# --------------------------------------------------------------------------- #
# Migration pre-check (§6a): which installed apps still carry a v1 receipt      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MigrationScan:
    """Result of scanning installed receipts for v1→release migration (§6a).

    ⚠️ ``pending`` (v1 receipts) and ``unreadable`` are kept apart on purpose:
    an app whose receipt could not be read is **not** the same as no app needing
    migration, and folding the two would let a corrupt receipt read as "clean"
    (installers owner, RELP2A2 追加1: unknown must not become empty).

    ``scan_error`` is set (non-``None``) when the scan could not even enumerate
    ``apps/`` -- "could not read" is not "nothing to migrate", and folding a
    listing failure into an empty scan is the same lie in a wider form (owner
    audit B2).

    ``broken_links`` lists dangling symlinks under ``apps/`` (their target is
    gone).  These are **not** ``unreadable``: a broken link is the debris a user
    leaves behind after deleting an app, and folding it into ``unreadable`` would
    park the P3 gate on a permanent false "unknown" (fable r3 追加1, the same
    trap as the ``.DS_Store`` regression).  They are reported for visibility but
    do not gate."""

    pending: tuple[str, ...]
    unreadable: tuple[str, ...]
    scan_error: str | None = None
    broken_links: tuple[str, ...] = ()


def scan_app_migrations(hyprial_home: Path) -> MigrationScan:
    """Which installed apps still carry a v1 (git) receipt, and which could not
    be read (§6a).

    Reads ``apps/*/install.json`` only -- cheap enough for every ``hyprial
    autoupdate status``.  A directory named ``install.json`` (another installer's
    ledger) or a missing receipt is *not* an installed hyprial app, so it is
    skipped silently; a receipt that fails to parse, is not an object, carries an
    unrecognised schema, or **cannot be read at all** (e.g. EACCES on its app
    directory) is surfaced in ``unreadable`` rather than silently dropped -- "read
    denied" is not the same as "absent" (owner audit B2).  A failure to list
    ``apps/`` itself is reported in ``scan_error`` rather than folded to empty.

    Symlinked app directories follow the same lens as ``mount`` (mount.py, which
    lists with ``Path.is_dir()`` -- following symlinks), so an app mount can see
    is never invisible here (fable r3).  A symlink is classified by its target
    (fable r3 追加1): pointing at a directory -> scanned as an app; a *dangling*
    link (target gone) -> ``broken_links`` (debris, not a false "unknown" -- it
    must not gate); a target that exists but cannot be resolved/read -> surfaced
    in ``unreadable``; a link to an existing readable non-directory -> skipped
    like a stray file (it is not an app)."""

    apps_root = hyprial_home / "apps"
    if not apps_root.is_dir():
        return MigrationScan((), ())
    try:
        # ``os.scandir`` (not ``Path.iterdir``) so the enumeration failure is a
        # catchable OSError here rather than a swallowed empty listing.
        with os.scandir(apps_root) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError as error:
        return MigrationScan((), (), scan_error=f"cannot list {apps_root}: {error}")
    pending: list[str] = []
    unreadable: list[str] = []
    broken_links: list[str] = []
    for entry in entries:
        app_name = entry.name
        try:
            # Only a *directory* can be an installed app.  ``mount`` lists apps
            # with ``Path.is_dir()``, which follows symlinks, so the scan must
            # follow them too -- otherwise a symlinked app mount can see stays
            # invisible here and ``pendingAppMigrations`` under-reports (the P3
            # gate reads exactly that field; fable r3).
            if entry.is_symlink():
                # Follow the link, matching mount.  ``is_dir(follow_symlinks=
                # True)`` returns True only when the target exists and is a
                # directory (a scanned app, below); it swallows FileNotFound
                # (dangling) into False but *raises* on EACCES resolving the
                # target -- so the raise means "exists but unreadable".
                if not entry.is_dir(follow_symlinks=True):
                    # Not a directory: either a dangling link (target gone) or a
                    # readable non-directory target.  ``os.stat`` (follows)
                    # tells them apart so we do not fold app debris into a false
                    # "unknown" (fable r3 追加1).
                    try:
                        os.stat(entry.path)
                    except FileNotFoundError:
                        # Dangling link -- typically debris left after a user
                        # deleted the app.  Report it, but ⛔ do NOT gate on it.
                        broken_links.append(app_name)
                    except OSError:
                        # Target exists but cannot be resolved/read: a real
                        # "unknown" -- surface it (owner audit B2).
                        unreadable.append(app_name)
                    # else: a link to an existing readable non-directory -- not
                    # an app, skipped like a stray file.
                    continue
            elif not entry.is_dir(follow_symlinks=False):
                # A stray plain file in apps/ (Darwin's ``.DS_Store``, an editor
                # swap file, ...) is not an app and must NOT be reported as
                # unreadable: ``os.stat`` on ``<file>/install.json`` raises
                # ENOTDIR, which would otherwise fold a harmless file into
                # ``unreadable`` -- a false "unknown" that blocks the P3 gate
                # (r1 regression / fable N1).  ``entry.is_dir`` reads the
                # directory type recorded by the parent listing, so a chmod-000
                # *directory* still classifies as a directory here (its
                # unreadability is caught by ``os.stat`` below, owner audit B2).
                continue
        except OSError:
            # Could not even determine the entry type -- surface, don't drop.
            unreadable.append(app_name)
            continue
        app_root = apps_root / app_name
        receipt_path = app_root / "install.json"
        try:
            # ``os.stat`` follows into the app directory, so EACCES on that
            # directory raises here instead of ``is_file()`` quietly returning
            # False (owner audit B2: on 3.14 a permission error otherwise makes a
            # real v1 receipt vanish from both tables).
            info = os.stat(receipt_path)
        except FileNotFoundError:
            # No receipt (and, because os.stat reached its parent, the app
            # directory itself is readable): not an installed hyprial app.
            continue
        except OSError:
            # Could not determine whether a receipt exists -- surface it.
            unreadable.append(app_name)
            continue
        if stat.S_ISDIR(info.st_mode):
            # A directory named install.json is another installer's ledger.
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        try:
            raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            unreadable.append(app_name)
            continue
        if not isinstance(raw, dict):
            unreadable.append(app_name)
            continue
        schema = raw.get("schema")
        if schema == _RECEIPT_SCHEMA_V1:
            pending.append(app_name)
        elif schema != _RECEIPT_SCHEMA_V2:
            # Readable, but neither v1 nor v2: cannot classify, so surface it
            # rather than assume it needs no migration.
            unreadable.append(app_name)
    return MigrationScan(
        tuple(pending),
        tuple(sorted(unreadable)),
        broken_links=tuple(sorted(broken_links)),
    )
