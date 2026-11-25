"""Archive downloads of the uv and CPython acquisition scripts, and the free-threaded variants.

2026-11-17: the 6 ``cpython-3...`` download failures came from ``uv python install
3.14t|3.15t`` after ``unset UV_PYTHON_INSTALL_MIRROR`` (nothing but 3.12 was
staged), and ``curl --retry`` does not retry a TLS handshake failure. The
scripts now retry the download with ci_retry_uv and stage the free-threaded
archives under the same pinned, atomic scheme. Everything runs offline against
fake archives served from a file:// release layout.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
MIRROR = ROOT / "scripts/ci/ci-ensure-cpython-mirror.sh"
ENSURE_UV = ROOT / "scripts/ci/ci-ensure-uv.sh"

ARCH = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine())
pytestmark = pytest.mark.skipif(ARCH is None, reason=f"unpinned arch {platform.machine()}")

FREE_THREADED_PINS = {
    ("3.14t", "3.14.4", "x86_64"): "5c539d5421ce145e10ea455f5b50729db1f7a5591198a75eabed87a8a8b9d30e",
    ("3.14t", "3.14.4", "aarch64"): "629dd91192c64255af852150f1aea16e98c8cc9a8a106dec9a9655c86906a920",
    ("3.15t", "3.15.0a8", "x86_64"): "555122ff5e95beabc7935977f528445e07806ce4caeff2052d22daf83b2b7d57",
    ("3.15t", "3.15.0a8", "aarch64"): "c6fc434b6af1f1bd9630ce3b4ad80079c684f23654ae926e3f064f1fa85058d3",
}


def _tarball(name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"fake\n"
        info = tarfile.TarInfo(name)
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _flaky_curl(tmp_path: Path, failures: int, rc: int = 35) -> tuple[Path, Path]:
    commands = tmp_path / "bin"
    commands.mkdir(exist_ok=True)
    calls = tmp_path / "curl.calls"
    real = subprocess.run(["bash", "-c", "command -v curl"], capture_output=True, text=True).stdout.strip()
    curl = commands / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        f"n=$(wc -l < {shlex.quote(str(calls))})\n"
        f"if [ \"$n\" -le {failures} ]; then echo 'curl: (35) TLS connect error' >&2; exit {rc}; fi\n"
        f"exec {shlex.quote(real)} \"$@\"\n"
    )
    curl.chmod(0o755)
    return commands, calls


def _run(script: Path, tmp_path: Path, *args: str, path_prefix: Path | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    path = os.environ["PATH"] if path_prefix is None else f"{path_prefix}:{os.environ['PATH']}"
    full = {
        "PATH": path,
        "HOME": str(tmp_path),
        "CI_CACHE_ROOT": str(tmp_path / "cache"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        "CI_RETRY_BACKOFF": "0 0",
        **env,
    }
    return subprocess.run(["bash", str(script), *args], env=full, capture_output=True, text=True, timeout=60)


def _release(tmp_path: Path, archive: str) -> tuple[Path, str]:
    payload = _tarball("python/bin/python")
    base = tmp_path / "release"
    (base / "20260414").mkdir(parents=True, exist_ok=True)
    (base / "20260414" / archive).write_bytes(payload)
    return base, hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(("variant", "version"), [("3.14t", "3.14.4"), ("3.15t", "3.15.0a8")])
def test_free_threaded_variant_stages_exactly_one_pinned_archive_and_exports_the_mirror(
    tmp_path: Path, variant: str, version: str
) -> None:
    archive = f"cpython-{version}+20260414-{ARCH}-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz"
    base, digest = _release(tmp_path, archive)
    result = _run(MIRROR, tmp_path, variant, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert f"CI_TOOL_CACHE miss tool=cpython version={version}+20260414" in result.stdout
    staged = sorted(p.name for p in (tmp_path / "cache/cpython/20260414").iterdir())
    assert staged == [archive]
    assert f"UV_PYTHON_INSTALL_MIRROR=file://{tmp_path}/cache/cpython" in (tmp_path / "github_env").read_text()


@pytest.mark.parametrize(("variant", "version"), [("3.14t", "3.14.4"), ("3.15t", "3.15.0a8")])
def test_free_threaded_builtin_pin_is_enforced_for_the_runner_arch(tmp_path: Path, variant: str, version: str) -> None:
    archive = f"cpython-{version}+20260414-{ARCH}-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz"
    base, _digest = _release(tmp_path, archive)
    result = _run(MIRROR, tmp_path, variant, CPYTHON_DOWNLOAD_BASE=f"file://{base}")
    assert result.returncode != 0
    assert f"does not match the pinned sha256 {FREE_THREADED_PINS[(variant, version, ARCH)]}" in result.stderr
    assert not any((tmp_path / "cache/cpython/20260414").iterdir())
    assert not (tmp_path / "github_env").exists()


def test_free_threaded_variant_without_cache_root_is_a_noop(tmp_path: Path) -> None:
    result = _run(MIRROR, tmp_path, "3.14t", CI_CACHE_ROOT="")
    assert result.returncode == 0, result.stderr
    assert "CI_CACHE_ROOT unset" in result.stdout
    assert not (tmp_path / "github_env").exists()


def test_unknown_variant_is_refused_before_any_download(tmp_path: Path) -> None:
    result = _run(MIRROR, tmp_path, "3.13t")
    assert result.returncode == 2
    assert "unknown variant" in result.stderr
    assert not (tmp_path / "cache").exists()


def test_cpython_download_is_retried_after_transient_failures_and_the_pin_still_decides(tmp_path: Path) -> None:
    archive = f"cpython-3.14.4+20260414-{ARCH}-unknown-linux-gnu-freethreaded-install_only_stripped.tar.gz"
    base, digest = _release(tmp_path, archive)
    commands, calls = _flaky_curl(tmp_path, failures=2)
    result = _run(
        MIRROR, tmp_path, "3.14t", path_prefix=commands,
        CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 3
    assert "CI_RETRY label=cpython-download-3.14t attempt=3/3 status=ok" in result.stderr


def test_cpython_download_exhaustion_fails_and_leaves_nothing_cached(tmp_path: Path) -> None:
    archive = f"cpython-3.12.13+20260414-{ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
    base, digest = _release(tmp_path, archive)
    commands, calls = _flaky_curl(tmp_path, failures=99)
    result = _run(
        MIRROR, tmp_path, path_prefix=commands,
        CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest,
    )
    assert result.returncode != 0
    assert len(calls.read_text().splitlines()) == 3
    assert "CI_RETRY label=cpython-download-default exhausted attempts=3 rc=35" in result.stderr
    assert not any((tmp_path / "cache/cpython/20260414").iterdir())
    assert not (tmp_path / "github_env").exists()


def test_uv_archive_download_is_retried_and_the_digest_check_is_not(tmp_path: Path) -> None:
    arch = f"{ARCH}-unknown-linux-gnu"
    archive = f"uv-{arch}.tar.gz"
    payload = _tarball("uv-x/uv")
    release = tmp_path / "release/9.9.9"
    release.mkdir(parents=True)
    (release / archive).write_bytes(payload)
    commands, calls = _flaky_curl(tmp_path, failures=2)
    env = {
        "UV_VERSION": "9.9.9",
        "UV_DOWNLOAD_BASE": f"file://{tmp_path}/release",
        "UV_BIN_DIR": str(tmp_path / "uvbin"),
        "UV_HOST_BIN": str(tmp_path / "no-such-uv"),
    }
    result = _run(ENSURE_UV, tmp_path, path_prefix=commands, UV_ARCHIVE_SHA256="0" * 64, **env)
    assert result.returncode != 0
    assert "does not match the pinned sha256" in result.stderr
    assert result.stderr.count("status=failed") == 2
    assert "CI_RETRY label=uv-archive-download attempt=3/3 status=ok" in result.stderr
    assert len(calls.read_text().splitlines()) == 3
    assert not any((tmp_path / "cache/uv/9.9.9").glob("uv-*.tar.gz"))
