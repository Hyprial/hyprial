"""The hyprial-tsnet sidecar: pinned constants, verification, and acquisition.

This is the **only** module where the sidecar's release coordinates may
appear as literals (spec U3b T13): the version, the four per-platform
sha256 values, and the binary repository URL live here and every other
module reaches them through the names below — a grep for any of the
literals must find exactly this file.

Where the numbers come from (measured 2026-09-10 by fetching each release
asset with the ``git archive --remote`` transport used by install at the time,
extracting its bytes, and computing sha256 locally; the release's
``SHA256SUMS`` was consulted only afterward for reconciliation — hyprial does
**not** recompute or re-derive the pins at runtime):

- ``tsnet-v0.1.4`` (target 4e6cef63; bin repo tag pushed by
  ``hyprial-ci``).  This build emits ``error`` before its slow
  ``backend.Close()`` (card 6cdcc406), which is what lets
  hyprial's join margin be 5 again.  ``tsnet-v0.1.2`` is the superseded
  Close-first build (kept in the bin repo by retention); ``tsnet-v0.1.0``
  was never published and ``tsnet-v0.1.1`` misreported ``0.1.0`` in
  hello — none of those may be pinned.
- **Download transport: anonymous HTTPS from the public GitHub release.**
  ``GET <SIDECAR_RELEASE_BASE_URL>/tsnet-v<version>/<asset>`` — one file,
  no clone, **no credentials** — from ``Hyprial/hyprial-tsnet-bin``
  (release ``tsnet-v<version>``, asset names ``hyprial-tsnet-<platform>``).
  The local installed filename is ``hyprial-tsnet`` as well (they are
  separate constants that happen to share a value; ⛔ do not collapse
  them).  The trust root is **still the pinned sha256**; the release's
  ``SHA256SUMS`` is for reconciliation only and is deliberately not
  consulted.  The base URL can be overridden with
  ``HYPRIAL_SIDECAR_RELEASE_BASE_URL`` (tests/E2E point it at a
  ``file://`` tree with the same ``<tag>/<asset>`` shape).

  ⭐ **This supersedes Allen's ruling E (2026-09-05, "git archive
  --remote, no HTTP").**  Superseded knowingly, by Allen on 2026-09-17,
  after being shown ruling E's text and this measurement — ⛔ not
  overlooked.  His words: 「可以,既然如此,走Https 取release资产」.

  ⚠️ **Ruling E could not be carried over by pointing the same command at
  GitHub**, which is how it reads at first (it names transport and address
  in one breath).  Measured 2026-09-17:

  - HTTPS → ``RPC failed; HTTP 422`` then
    ``fatal: git archive: expected ACK/NAK, got a flush packet``
  - SSH → ``Invalid command: git-upload-archive
    '/Hyprial/hyprial-tsnet-bin.git'``
  - positive control, same command shape against the internal repo →
    ``exit=0`` and a real 10240-byte tar holding the four pinned digests,
    so the failures above are GitHub's refusal, ⛔ not a malformed command.

  🔑 And the deeper reason, which no amount of GitHub support would fix:
  anonymous public download is **HTTPS-only** (``git://`` was withdrawn in
  2022; SSH requires an account and key).  "No HTTP" and "a customer with
  no credentials can download it" are mechanically incompatible — so the
  question was never *whether* to leave ruling E, only *how*.

Two consumers share this module:

- ``hyprial login`` — the consent-shaped download step (:func:`install_sidecar`):
  show the source (URL + tag), version, and expected sha256, interactive
  y/N (``--install-sidecar`` to bypass y/N), stream to a temporary file,
  verify sha256,
  ``chmod 0755``, and atomically rename into ``$HYPRIAL_HOME/bin/hyprial-tsnet``.
  A mismatch deletes the temporary file and leaves no partial binary
  behind.
- ``hyprial login`` and its join stage share
  :func:`verify_installed_sidecar`: re-check the sha256 before acquisition
  or start, then let the join stage compare ``hello.sidecar`` against
  :data:`SIDECAR_VERSION`.  A missing or mismatched binary may be acquired by
  login after the consent step; non-interactive login reports
  ``SIDECAR_MISSING``/``SIDECAR_MISMATCH`` with a pointer to
  ``hyprial login --install-sidecar`` instead of prompting.

The **download transport** is deliberately one replaceable function,
:func:`fetch_sidecar_asset` (version, asset name, temporary destination →
file): everything above it — consent, sha256 pin, chmod, atomic rename,
failure cleanup — is transport-agnostic and pinned by tests.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import platform
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


__all__ = [
    "SIDECAR_ASSET_BASENAME",
    "SIDECAR_BIN_DIRNAME",
    "SIDECAR_BIN_FILENAME",
    "SIDECAR_RELEASE_BASE_URL",
    "SIDECAR_RELEASE_BASE_URL_ENV",
    "SIDECAR_RETIRED_OVERRIDE_ENV",
    "SIDECAR_SHA256",
    "SIDECAR_VERSION",
    "SidecarError",
    "current_platform",
    "expected_sha256",
    "fetch_sidecar_asset",
    "install_sidecar",
    "sha256_file",
    "sidecar_asset_url",
    "sidecar_release_base_url",
    "sidecar_tag",
    "sidecar_binary_path",
    "verify_installed_sidecar",
]

SIDECAR_VERSION = "0.1.4"
"""The pinned hyprial-tsnet sidecar version (harness-bridge tag
``tsnet-v0.1.4`` — target 4e6cef63; emits ``error`` before its slow Close,
card 6cdcc406).

``hello.sidecar`` must equal this exactly before the join proceeds
(spec §1.1 / protocol §5)."""

SIDECAR_SHA256: dict[str, str] = {
    "darwin-amd64": "c05faf11d6df6083e9ea2afad46574a067fff560581f5dbdf14de985caecd92e",
    "darwin-arm64": "fdbcd151ff68254562ccaae470e59c3441a49c4b41cd0534a5b7c729e41cb9a2",
    "linux-amd64": "10abb550c59ce439a83ddbed579311ae5b074ec66c4cd985497f2aa8d1569bc4",
    "linux-arm64": "1b499cb0d2d66b1cff27a70c42cd44a08369ef18ab37b09b9a8475672ce1afd9",
}
"""Per-platform sha256 measured from assets fetched on 2026-09-10 with
acquisition's then-current ``git archive --remote`` transport and computed
locally; the release's ``SHA256SUMS`` was checked only afterward for
reconciliation.  hyprial never recomputes these at runtime.

⭐ The 2026-09-17 move to HTTPS release assets changed **how the bytes
travel**, ⛔ not what they must hash to: these values are unchanged and
remain the sole trust root."""

SIDECAR_RELEASE_BASE_URL = (
    "https://github.com/Hyprial/hyprial-tsnet-bin/releases/download"
)
"""Base URL for the public release assets; the full URL is
``<base>/tsnet-v<SIDECAR_VERSION>/<asset>``.

Release ``tsnet-v<SIDECAR_VERSION>`` carries the four platform assets plus
``SHA256SUMS`` (reconciliation only — the pinned :data:`SIDECAR_SHA256` is
the trust root).  Overridable via :data:`SIDECAR_RELEASE_BASE_URL_ENV`.

⚠️ The binaries exist **only as release assets**: the public repository's
git history holds nothing but a README, so any "clone it and take the
file" approach returns an empty tree."""

SIDECAR_RELEASE_BASE_URL_ENV = "HYPRIAL_SIDECAR_RELEASE_BASE_URL"
"""Environment override for :data:`SIDECAR_RELEASE_BASE_URL`.

⭐ Accepts ``https://`` **and** ``file://`` — deliberately, because that is
the property the old override relied on.  The previous seam worked for
tests only because ``git archive --remote`` happened to accept local
paths; when the transport moved to HTTPS that sentence stopped being true,
so the replacement seam has to carry the local-path property explicitly
rather than inherit it from the transport."""

SIDECAR_RETIRED_OVERRIDE_ENV = "HYPRIAL_SIDECAR_BIN_REPO"
"""The pre-2026-09-17 override (a git repo URL or local path).

⛔ Deliberately **not** honored and **not** silently ignored.  Its values
were git remotes, which mean nothing to an HTTPS release download, so it
cannot be reinterpreted.  And ignoring it would be worse than failing: an
operator who pointed it at the internal repository would be sent to the
public release without being told.  Setting it is therefore a loud
``SIDECAR_OVERRIDE_RETIRED`` error naming the replacement — which also
means any harness this migration missed fails visibly instead of quietly
downloading from the internet."""

SIDECAR_BIN_DIRNAME = "bin"
# ⛔ These two constants hold the same string today.  That is a *result* of
# #440 renaming the local binary, ⛔ not a requirement -- they exist as two
# constants precisely so they *can* differ (the remote asset name moves with
# the release; the local filename moves with the install).
# ⇒ Before you make them differ again -- or before you reach for "either one,
#   they are the same" -- read
#   tests/test_tsnet_sidecar.py::test_release_coordinates_pin_both_names_which_are_now_equal
#   ⭐ While they are equal, a value-based check cannot tell them apart; the
#   only thing that can is *which* tests go red when one is mutated to a
#   unique sentinel (asset side: 1 use site, local side: 6).
SIDECAR_ASSET_BASENAME = "hyprial-tsnet"
"""Basename of the *remote* release assets (``<basename>-<platform>`` inside
tag ``tsnet-v<version>``).

⚠️ Since the hyprial rename this happens to equal
:data:`SIDECAR_BIN_FILENAME`.  They are still **two different coordinates**:
this one names a file *in the release*, that one names a file *on this
machine*.  ⛔ Do not merge them, and ⛔ do not assert one by matching the
other's value — while they are equal, a value-based assertion cannot tell
which of the two it is talking about."""

SIDECAR_BIN_FILENAME = "hyprial-tsnet"

_DOWNLOAD_TIMEOUT_S = 600.0
"""Per-read socket timeout for one asset download (urllib semantics).

It bounds how long a single read may wait for data, **not** the whole
transfer, so a slow but live connection is never cut off."""

_PROBE_TIMEOUT_S = 30.0
"""Budget for the one extra request that splits a 404 into its two causes."""

Plan = dict[str, Any]

_DOWNLOAD_CHUNK = 1024 * 1024


class SidecarError(RuntimeError):
    """A stable sidecar acquisition failure; ``code``/``data`` feed the CLI."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}


# -- coordinates ---------------------------------------------------------------


def sidecar_binary_path(hyprial_home: Path) -> Path:
    """``$HYPRIAL_HOME/bin/hyprial-tsnet`` — where install lands and login starts."""

    return Path(hyprial_home) / SIDECAR_BIN_DIRNAME / SIDECAR_BIN_FILENAME


def current_platform() -> str:
    """The asset platform key for this machine (one of SIDECAR_SHA256's)."""

    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        architecture = "arm64"
    elif machine in ("amd64", "x86_64"):
        architecture = "amd64"
    else:
        raise SidecarError(
            "SIDECAR_PLATFORM_UNSUPPORTED",
            f"no hyprial-tsnet asset for machine architecture {machine!r}",
        )
    key = f"{system}-{architecture}"
    if key not in SIDECAR_SHA256 and not (
        os.environ.get("HYPRIAL_BUNDLED_TSNET_BINARY") and key in BUNDLED_SIDECAR_SHA256
    ):
        raise SidecarError(
            "SIDECAR_PLATFORM_UNSUPPORTED",
            f"no hyprial-tsnet asset for platform {key!r}; "
            f"available: {sorted(SIDECAR_SHA256)}",
        )
    return key


# Internal desktop overrides never change the public release acquisition pins.
BUNDLED_SIDECAR_SHA256 = {
    "darwin-arm64": "e3850c0ec5535dd2f4fc6a69a3caf08745a8ee58a3fdd1067aea26b8f67a2c8b",
    "windows-amd64": "0653bc44093578acb02936df12f0417e20a74a87aedb2582e2c58328bd85b2c2",
}

def expected_bundled_sha256(platform_key: str) -> str:
    return BUNDLED_SIDECAR_SHA256.get(platform_key) or expected_sha256(platform_key)


def expected_sha256(platform_key: str) -> str:
    """The pinned sha256 for a platform key (loud on an unknown one)."""

    try:
        return SIDECAR_SHA256[platform_key]
    except KeyError:
        raise SidecarError(
            "SIDECAR_PLATFORM_UNSUPPORTED",
            f"unknown sidecar platform {platform_key!r}; "
            f"available: {sorted(SIDECAR_SHA256)}",
        ) from None


def _reject_retired_override(environ: Mapping[str, str] | None) -> None:
    """Refuse loudly when the retired git-remote override is still set."""

    env = os.environ if environ is None else environ
    value = env.get(SIDECAR_RETIRED_OVERRIDE_ENV, "").strip()
    if value:
        raise SidecarError(
            "SIDECAR_OVERRIDE_RETIRED",
            f"{SIDECAR_RETIRED_OVERRIDE_ENV} is no longer read: the sidecar is "
            "now downloaded over HTTPS from a release, so a git remote cannot "
            f"be used.  Set {SIDECAR_RELEASE_BASE_URL_ENV} to a base URL "
            "(https:// or file://) serving <tag>/<asset> instead, and unset "
            f"{SIDECAR_RETIRED_OVERRIDE_ENV}",
            {
                "retired": SIDECAR_RETIRED_OVERRIDE_ENV,
                "value": value,
                "replacement": SIDECAR_RELEASE_BASE_URL_ENV,
            },
        )


def sidecar_release_base_url(
    environ: Mapping[str, str] | None = None,
) -> str:
    """The release base URL this install fetches from (env override honored)."""

    env = os.environ if environ is None else environ
    value = env.get(SIDECAR_RELEASE_BASE_URL_ENV, "").strip()
    return (value or SIDECAR_RELEASE_BASE_URL).rstrip("/")


def sidecar_asset_url(
    version: str | None = None,
    asset: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The full download URL for one platform asset."""

    name = asset or f"{SIDECAR_ASSET_BASENAME}-{current_platform()}"
    base = sidecar_release_base_url(environ)
    return f"{base}/{sidecar_tag(version)}/{name}"


def sidecar_tag(version: str | None = None) -> str:
    """The release tag carrying one sidecar version: ``tsnet-v<version>``."""

    return f"tsnet-v{version or SIDECAR_VERSION}"


def sha256_file(path: Path) -> str:
    """Streamed sha256 hex digest of a file (no full-file read)."""

    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_installed_sidecar(hyprial_home: Path) -> tuple[Path | None, str | None]:
    """Login-side pre-start check: ``(path, None)`` or ``(None, reason)``.

    ``reason`` is ``SIDECAR_MISSING`` (no binary) or ``SIDECAR_MISMATCH``
    (sha256 differs from the pin) — both point the operator at
    ``hyprial login --install-sidecar`` and neither ever downloads anything.
    The hello
    handshake's ``sidecar == SIDECAR_VERSION`` comparison happens after
    start, in the join stage; this is the cheap gate before spawning.
    """

    bundled = os.environ.get("HYPRIAL_BUNDLED_TSNET_BINARY")
    path = Path(bundled) if bundled is not None else sidecar_binary_path(hyprial_home)
    if bundled is not None and not path.is_absolute():
        return None, "SIDECAR_MISMATCH"
    try:
        platform_key = current_platform()
    except SidecarError as error:
        return None, error.code
    if not path.is_file():
        return None, "SIDECAR_MISSING"
    expected = expected_bundled_sha256(platform_key) if bundled is not None else expected_sha256(platform_key)
    if sha256_file(path) != expected:
        return None, "SIDECAR_MISMATCH"
    return path, None


# -- acquisition (hyprial login's consent-shaped step) -------------------------------


def install_sidecar(
    hyprial_home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    confirm: Callable[[Plan], bool],
    json_output: bool = False,
) -> Plan:
    """Fetch, verify, and atomically place the pinned sidecar binary.

    Consent shape mirrors ``install_application``: the plan (source URL +
    tag, version, expected sha256, destination) is shown first and
    ``confirm`` decides;
    ⛔ no silent download (spec T10).  ``confirm`` returning False is a
    recorded decline, not an error — the caller may already have succeeded
    at its own task (the application install) and must not be failed
    retroactively.  A sha256 mismatch raises :class:`SidecarError` with the
    temporary file deleted and nothing landed.  If the destination already
    matches the pin, the step is a no-op reported as ``alreadyCurrent``
    (nothing to consent to — the binary is already exactly the pinned one).
    """

    env = os.environ if environ is None else environ
    platform_key = current_platform()
    asset = f"{SIDECAR_ASSET_BASENAME}-{platform_key}"
    expected = expected_sha256(platform_key)
    destination = sidecar_binary_path(hyprial_home)
    source = sidecar_asset_url(SIDECAR_VERSION, asset, environ=env)
    tag = sidecar_tag()

    if destination.is_file() and sha256_file(destination) == expected:
        return {
            "ok": True,
            "installed": True,
            "alreadyCurrent": True,
            "name": SIDECAR_BIN_FILENAME,
            "version": SIDECAR_VERSION,
            "destination": str(destination),
        }

    # After the already-current return: a lingering retired variable must
    # not break a login that has nothing to download.
    _reject_retired_override(env)
    plan: Plan = {
        "ok": True,
        "name": SIDECAR_BIN_FILENAME,
        "version": SIDECAR_VERSION,
        "platform": platform_key,
        "source": source,
        "tag": tag,
        "sha256": expected,
        "destination": str(destination),
    }
    if not confirm(plan):
        return {
            "ok": True,
            "installed": False,
            "declined": True,
            "name": SIDECAR_BIN_FILENAME,
            "version": SIDECAR_VERSION,
            "destination": str(destination),
        }

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / (
        f".{SIDECAR_BIN_FILENAME}.download-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        # The transport seam: everything below (sha256 verify, chmod,
        # atomic rename, failure cleanup) is transport-agnostic and must
        # keep working when the transport is swapped.
        fetch_sidecar_asset(SIDECAR_VERSION, asset, temporary, environ=env)
        actual = sha256_file(temporary)
        if actual != expected:
            raise SidecarError(
                "SIDECAR_SHA_MISMATCH",
                f"fetched {asset}@{tag} from {source} does not match the "
                f"pinned sha256 for {platform_key}; expected {expected}, "
                f"got {actual}; nothing was installed",
                {
                    "source": source,
                    "tag": tag,
                    "expected": expected,
                    "actual": actual,
                },
            )
        os.chmod(temporary, 0o755)
        os.replace(temporary, destination)
    except SidecarError:
        temporary.unlink(missing_ok=True)
        raise
    except BaseException:
        # Any other failure (network, disk) must leave no partial binary.
        temporary.unlink(missing_ok=True)
        raise
    return {
        "ok": True,
        "installed": True,
        "alreadyCurrent": False,
        "name": SIDECAR_BIN_FILENAME,
        "version": SIDECAR_VERSION,
        "platform": platform_key,
        "source": source,
        "tag": tag,
        "sha256": expected,
        "destination": str(destination),
        "installedAt": datetime.now(UTC).isoformat(),
    }


def fetch_sidecar_asset(
    version: str,
    asset: str,
    destination: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Land one release asset at ``destination``; returns the file written.

    ⭐ **The one transport seam**: inputs are the version, the platform
    asset name, and a temporary destination path — how the bytes travel is
    decided *here and only here*.  The transport is an anonymous HTTPS
    ``GET <base>/tsnet-v<version>/<asset>`` against the public release
    (see the module docstring for why ruling E's ``git archive --remote``
    could not be carried over).  The base is :data:`SIDECAR_RELEASE_BASE_URL`,
    overridable via :data:`SIDECAR_RELEASE_BASE_URL_ENV` (``file://`` too, so
    tests and E2E can serve a local tree with the same shape).

    ⛔ No credentials, ever: a customer's machine has none, so the only path
    that proves a customer can install is the anonymous one.  Failures are
    loud and **split by cause**, because the same HTTP status can mean
    opposite things:

    - ``SIDECAR_RELEASE_NOT_FOUND`` — 404, and the release tag itself is
      absent: this version was never published.
    - ``SIDECAR_ASSET_NOT_FOUND`` — 404, but the release exists: the asset
      name is wrong or that platform's build is missing.
    - ``SIDECAR_DOWNLOAD_NOT_FOUND`` — 404 where the follow-up probe could
      not tell the two apart; the message names both causes.
    - ``SIDECAR_DOWNLOAD_FORBIDDEN`` — 403 (visibility, rate limit, or a
      draft release).
    - ``SIDECAR_DOWNLOAD_FAILED`` — any other HTTP status.
    - ``SIDECAR_DOWNLOAD_UNREACHABLE`` — no HTTP response at all: DNS,
      proxy, TLS, or connection failure.
    - ``SIDECAR_DOWNLOAD_INCOMPLETE`` — the body ended early (fewer bytes
      than Content-Length, or the connection dropped mid-transfer).  Named
      apart from ``SIDECAR_SHA_MISMATCH`` on purpose: a truncated file is a
      network problem, and must not be reported as a content mismatch.
    - ``SIDECAR_DOWNLOAD_TIMEOUT`` — no data within the per-read timeout.
      The timeout bounds each read, not the total: a slow link that keeps
      delivering bytes is never cut off (a 30 MB asset was measured at
      3.2 MB per 180 s on a direct connection).
    - ``SIDECAR_OVERRIDE_RETIRED`` — the pre-HTTPS git-remote override is
      still set (see :data:`SIDECAR_RETIRED_OVERRIDE_ENV`).

    Whatever arrives is only ever trusted after the caller's sha256 check —
    the pinned digest is the trust root, the release's ``SHA256SUMS`` is
    reconciliation only.
    """

    _reject_retired_override(environ)
    tag = sidecar_tag(version)
    base = sidecar_release_base_url(environ)
    url = f"{base}/{tag}/{asset}"
    context = {"source": url, "tag": tag, "asset": asset}
    request = urllib.request.Request(url, headers={"User-Agent": "hyprial"})
    written = 0
    expected_size: int | None = None
    try:
        with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_S) as response:
            expected_size = _content_length(response)
            with open(destination, "wb") as target:
                while chunk := response.read(_DOWNLOAD_CHUNK):
                    target.write(chunk)
                    written += len(chunk)
    except urllib.error.HTTPError as error:
        destination.unlink(missing_ok=True)
        raise _http_failure(error.code, base, tag, asset, context) from error
    except urllib.error.URLError as error:
        destination.unlink(missing_ok=True)
        if isinstance(error.reason, FileNotFoundError):
            # A ``file://`` base reports a missing file this way rather than
            # as an HTTPError; it is the same fact as an HTTP 404 and must
            # take the same cause-splitting path, or the local seam would
            # tell tests "unreachable" where production says "not found".
            raise _http_failure(404, base, tag, asset, context) from error
        if isinstance(error.reason, TimeoutError):
            raise _timeout_failure(context, written) from error
        raise SidecarError(
            "SIDECAR_DOWNLOAD_UNREACHABLE",
            f"could not reach {url} to fetch the hyprial-tsnet sidecar "
            f"({error.reason}); check network or proxy settings, or place "
            "the sidecar binary at the destination by hand",
            {**context, "detail": str(error.reason)},
        ) from error
    except TimeoutError as error:
        destination.unlink(missing_ok=True)
        raise _timeout_failure(context, written) from error
    except (http.client.IncompleteRead, ConnectionError) as error:
        destination.unlink(missing_ok=True)
        raise _incomplete_failure(
            context, written, expected_size, str(error)
        ) from error
    if expected_size is not None and written != expected_size:
        destination.unlink(missing_ok=True)
        raise _incomplete_failure(
            context, written, expected_size, "fewer bytes than Content-Length"
        )
    return destination


def _content_length(response: Any) -> int | None:
    """The declared body size, or None when the server did not declare one."""

    raw = response.headers.get("Content-Length")
    try:
        value = int(raw) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and value >= 0 else None


def _incomplete_failure(
    context: dict[str, Any], written: int, expected: int | None, detail: str
) -> SidecarError:
    """A download that ended early: a network problem, never a content verdict.

    Size is used here only to *name* the failure.  Whether the bytes are
    trusted is decided by the pinned sha256 alone; a truncated file must not
    reach that check, or it would be reported as SIDECAR_SHA_MISMATCH -- which
    reads as tampering to whoever sees it.
    """

    of = f" of {expected}" if expected is not None else ""
    return SidecarError(
        "SIDECAR_DOWNLOAD_INCOMPLETE",
        f"the download of {context['source']} was cut short ({written}{of} "
        "bytes); this is a network interruption, not a content mismatch -- "
        "retry, ideally on a faster or proxied connection",
        {
            **context,
            "bytesWritten": written,
            "expectedBytes": expected,
            "detail": detail,
        },
    )


def _timeout_failure(context: dict[str, Any], written: int) -> SidecarError:
    """No data within the per-read timeout (not a total-duration cap)."""

    return SidecarError(
        "SIDECAR_DOWNLOAD_TIMEOUT",
        f"the download of {context['source']} stalled: no data for "
        f"{_DOWNLOAD_TIMEOUT_S:g}s after {written} bytes; this is a network "
        "problem, not a content mismatch -- retry, ideally on a faster or "
        "proxied connection",
        {**context, "bytesWritten": written, "timeoutSeconds": _DOWNLOAD_TIMEOUT_S},
    )


def _http_failure(
    status: int,
    base: str,
    tag: str,
    asset: str,
    context: dict[str, Any],
) -> SidecarError:
    """Turn one HTTP status into the error code that names its cause."""

    url = context["source"]
    data = {**context, "status": status}
    if status == 403:
        return SidecarError(
            "SIDECAR_DOWNLOAD_FORBIDDEN",
            f"{url} refused the download (HTTP 403); the release may be "
            "private or still a draft, or the host is rate limiting",
            data,
        )
    if status != 404:
        return SidecarError(
            "SIDECAR_DOWNLOAD_FAILED",
            f"downloading {url} failed with HTTP {status}",
            data,
        )
    exists = _release_exists(base, tag)
    if exists is False:
        return SidecarError(
            "SIDECAR_RELEASE_NOT_FOUND",
            f"release {tag} is not published (HTTP 404 for {url}, and the "
            f"release {tag} itself does not exist); this hyprial pins a "
            "sidecar version that has not been released yet",
            {**data, "releaseExists": False},
        )
    if exists is True:
        return SidecarError(
            "SIDECAR_ASSET_NOT_FOUND",
            f"release {tag} exists but has no asset {asset!r} (HTTP 404 for "
            f"{url}); the asset name is wrong or this platform's build is "
            "missing from the release",
            {**data, "releaseExists": True},
        )
    return SidecarError(
        "SIDECAR_DOWNLOAD_NOT_FOUND",
        f"HTTP 404 for {url}: either release {tag} is not published, or it "
        f"has no asset {asset!r} (these have opposite fixes, and the "
        "release could not be probed to tell them apart)",
        {**data, "releaseExists": None},
    )


def _release_exists(base: str, tag: str) -> bool | None:
    """Probe whether release ``tag`` exists, to split a 404 by cause.

    For a ``file://`` base the release is its ``<tag>/`` directory.  Returns
    ``None`` whenever the answer is not actually known — an HTTP(S) base that
    is not GitHub-shaped ``…/releases/download``, or a probe that itself
    fails.  ⛔ An unknown is never coerced into a yes or a
    no: the caller reports both causes instead of guessing one.
    """

    if base.startswith("file://"):
        # The local seam answers the same question the same way: a release
        # is its ``<tag>/`` directory.
        root = urllib.request.url2pathname(urllib.parse.urlparse(base).path)
        return Path(root, tag).is_dir()
    if not base.startswith(("https://", "http://")):
        return None
    head, _, last = base.rpartition("/")
    if last != "download":
        return None
    probe = urllib.request.Request(
        f"{head}/tag/{tag}", headers={"User-Agent": "hyprial"}
    )
    try:
        with urllib.request.urlopen(probe, timeout=_PROBE_TIMEOUT_S):
            return True
    except urllib.error.HTTPError as error:
        return False if error.code == 404 else None
    except (urllib.error.URLError, OSError):
        return None
