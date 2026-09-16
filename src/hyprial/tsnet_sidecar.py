"""The hyprial-tsnet sidecar: pinned constants, verification, and acquisition.

This is the **only** module where the sidecar's release coordinates may
appear as literals (spec U3b T13): the version, the four per-platform
sha256 values, and the binary repository URL live here and every other
module reaches them through the names below — a grep for any of the
literals must find exactly this file.

Where the numbers come from (measured 2026-09-10 by fetching each release
asset with the same ``git archive --remote`` command used by install,
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
- **Download transport (Allen's ruling E, 2026-09-05): no HTTP.**  The
  binary comes over the same git+ssh the user already installed hyprial with:
  ``git archive --remote=<SIDECAR_BIN_REPO> tsnet-v<version> <asset>`` —
  one file, no clone — from the dedicated binary repo
  ``HyprialOS/hyprial-tsnet-bin`` (tag ``tsnet-v<version>``, remote asset
  names ``hyprial-tsnet-<platform>``).  The local installed filename is
  ``hyprial-tsnet`` as well (they are separate constants that happen to
  share a value; ⛔ do not collapse them).  The trust root is **still the
  pinned sha256**; the repo's
  ``SHA256SUMS`` is for reconciliation only and is deliberately not
  consulted.  The repo can be overridden with ``HYPRIAL_SIDECAR_BIN_REPO``
  (tests/E2E point it at a local bare repo with the same tag shape).
  ⛔ No credentials are invented: ssh uses the operator's existing
  key/agent (``git_env()`` enforces BatchMode so it can never hang on a
  prompt), and a missing ``git`` or a failed fetch is a loud error code,
  never a silent fallback.

Two consumers share this module:

- ``hyprial login`` — the consent-shaped download step (:func:`install_sidecar`):
  show the source (repo + tag), version, and expected sha256, interactive
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
import os
import platform
import posixpath
import shutil
import subprocess
import tarfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.updates import git_env

__all__ = [
    "SIDECAR_ASSET_BASENAME",
    "SIDECAR_BIN_DIRNAME",
    "SIDECAR_BIN_FILENAME",
    "SIDECAR_BIN_REPO",
    "SIDECAR_BIN_REPO_ENV",
    "SIDECAR_SHA256",
    "SIDECAR_VERSION",
    "SidecarError",
    "current_platform",
    "expected_sha256",
    "fetch_sidecar_asset",
    "install_sidecar",
    "sha256_file",
    "sidecar_bin_repo",
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
acquisition's ``git archive --remote`` transport and computed locally; the
release's ``SHA256SUMS`` was checked only afterward for reconciliation.
hyprial never recomputes these at runtime."""

SIDECAR_BIN_REPO = "ssh://git@git.internal.hyprial.com/HyprialOS/hyprial-tsnet-bin.git"
"""The sidecar binary repository (ruling E: git archive --remote, no HTTP).

Tag ``tsnet-v<SIDECAR_VERSION>`` carries the four platform assets plus
``SHA256SUMS`` (reconciliation only — the pinned :data:`SIDECAR_SHA256` is
the trust root).  Overridable via :data:`SIDECAR_BIN_REPO_ENV` so tests and
E2E can point at a local bare repo with the same tag shape."""

SIDECAR_BIN_REPO_ENV = "HYPRIAL_SIDECAR_BIN_REPO"
"""Environment override for :data:`SIDECAR_BIN_REPO` (any git repo URL or
local path; ``git archive --remote`` accepts both)."""

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

_GIT_ARCHIVE_TIMEOUT_S = 600.0
"""Wall-clock budget for one ``git archive --remote`` — tens of MB over
ssh; BatchMode (git_env) already prevents prompt hangs."""

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
    if key not in SIDECAR_SHA256:
        raise SidecarError(
            "SIDECAR_PLATFORM_UNSUPPORTED",
            f"no hyprial-tsnet asset for platform {key!r}; "
            f"available: {sorted(SIDECAR_SHA256)}",
        )
    return key


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


def sidecar_bin_repo(
    environ: Mapping[str, str] | None = None,
) -> str:
    """The binary repo this install fetches from (env override honored)."""

    env = os.environ if environ is None else environ
    return env.get(SIDECAR_BIN_REPO_ENV, "").strip() or SIDECAR_BIN_REPO


def sidecar_tag(version: str | None = None) -> str:
    """The repo tag carrying one sidecar version: ``tsnet-v<version>``."""

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

    path = sidecar_binary_path(hyprial_home)
    try:
        platform_key = current_platform()
    except SidecarError as error:
        return None, error.code
    if not path.is_file():
        return None, "SIDECAR_MISSING"
    if sha256_file(path) != expected_sha256(platform_key):
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

    Consent shape mirrors ``install_application``: the plan (source repo +
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
    repo = sidecar_bin_repo(env)
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

    plan: Plan = {
        "ok": True,
        "name": SIDECAR_BIN_FILENAME,
        "version": SIDECAR_VERSION,
        "platform": platform_key,
        "source": repo,
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
                f"fetched {asset}@{tag} from {repo} does not match the "
                f"pinned sha256 for {platform_key}; expected {expected}, "
                f"got {actual}; nothing was installed",
                {
                    "source": repo,
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
        "source": repo,
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
    decided *here and only here*.  Ruling E (Allen, 2026-09-05): **no
    HTTP** — ``git archive --remote=<repo> tsnet-v<version> <asset>``,
    a single file over the same git+ssh used to install hyprial, no clone.
    The repo is :data:`SIDECAR_BIN_REPO` (overridable via
    :data:`SIDECAR_BIN_REPO_ENV` — tests and E2E point it at a local bare
    repo with the same tag shape; ``git archive --remote`` accepts local
    paths too).

    ⛔ No invented credentials: ssh uses the operator's existing
    key/agent; ``git_env()`` forces BatchMode so a missing key fails fast
    instead of hanging on a prompt.  Failures are loud:
    ``SIDECAR_GIT_UNAVAILABLE`` (no git on PATH), ``SIDECAR_ARCHIVE_FAILED``
    (git archive non-zero: ssh/auth/repo/tag/asset problems — the stderr
    tail travels in ``data``), ``SIDECAR_ARCHIVE_MALFORMED`` (the archive
    did not contain exactly the requested asset).  Whatever arrives is
    only ever trusted after the caller's sha256 check — the pinned digest
    is the trust root, the repo's ``SHA256SUMS`` is reconciliation only.
    """

    repo = sidecar_bin_repo(environ)
    tag = sidecar_tag(version)
    argv = ["git", "archive", f"--remote={repo}", tag, asset]
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
        )
    except FileNotFoundError as error:
        raise SidecarError(
            "SIDECAR_GIT_UNAVAILABLE",
            "git is required to fetch the hyprial-tsnet sidecar (the same "
            "git+ssh hyprial was installed with) but was not found on PATH; "
            "install git, or place the sidecar binary at the destination "
            "by hand",
            {"source": repo, "tag": tag, "asset": asset},
        ) from error
    stderr_chunks: list[bytes] = []
    drain = threading.Thread(
        target=lambda: stderr_chunks.append(
            process.stderr.read() if process.stderr is not None else b""
        ),
        daemon=True,
        name="sidecar-archive-stderr",
    )
    drain.start()
    shape_error: SidecarError | None = None
    try:
        _write_member_from_archive(process.stdout, asset, destination)
    except SidecarError as error:
        # A non-zero git exit explains an empty/truncated stream better
        # than the tar shape ever could — hold the shape error and let the
        # returncode check below decide which one the operator sees.
        shape_error = error
    finally:
        try:
            process.wait(timeout=_GIT_ARCHIVE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        drain.join(timeout=5.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
    detail = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
    if process.returncode != 0:
        raise SidecarError(
            "SIDECAR_ARCHIVE_FAILED",
            f"git archive could not fetch {asset}@{tag} from {repo}"
            + (f": {detail[-500:]}" if detail else ""),
            {"source": repo, "tag": tag, "asset": asset, "detail": detail[-500:]},
        )
    if shape_error is not None:
        raise shape_error
    return destination


def _write_member_from_archive(
    stream: Any,
    asset: str,
    destination: Path,
) -> None:
    """Extract the one ``asset`` member from a streaming tar to ``destination``.

    ``git archive`` prefixes member names with the tree-ish basename
    (``tsnet-v0.1.4/<asset>``), so members are matched by basename.  The
    archive must yield exactly one matching regular file — anything else
    is ``SIDECAR_ARCHIVE_MALFORMED`` rather than a guessed extraction.
    """

    names: list[str] = []
    written = False
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                names.append(member.name)
                if posixpath.basename(member.name) != asset or not member.isfile():
                    continue
                if written:
                    raise SidecarError(
                        "SIDECAR_ARCHIVE_MALFORMED",
                        f"the archive contains more than one {asset!r} member",
                        {"members": names},
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise SidecarError(
                        "SIDECAR_ARCHIVE_MALFORMED",
                        f"archive member {member.name!r} has no content",
                        {"members": names},
                    )
                with open(destination, "wb") as target:
                    shutil.copyfileobj(source, target, _DOWNLOAD_CHUNK)
                written = True
    except tarfile.TarError as error:
        raise SidecarError(
            "SIDECAR_ARCHIVE_MALFORMED",
            f"git archive output is not a usable tar stream: {error}",
            {"members": names},
        ) from error
    if not written:
        raise SidecarError(
            "SIDECAR_ARCHIVE_MALFORMED",
            f"the archive contains no {asset!r} member (got {names})",
            {"members": names},
        )
