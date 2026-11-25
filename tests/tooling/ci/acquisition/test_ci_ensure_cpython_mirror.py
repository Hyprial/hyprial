'scripts/ci/ci-ensure-cpython-mirror.sh: cache-first, sha256-pinned CPython for uv.\n\nSame #941 semantics as tests/tooling/ci/acquisition/test_ci_ensure_uv.py: the cached\npython-build-standalone archive is trusted only when its digest equals the\npin, so a half-written or corrupted file on the shared cache volume is\nre-downloaded, never reused; a download that fails the pin is never cached.\nOn success the script points uv at $CI_CACHE_ROOT/cpython as a file://\nUV_PYTHON_INSTALL_MIRROR (verified locally against uv 0.11.7).  Runs offline\nagainst a fake archive served over file://.\n'

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/ci-ensure-cpython-mirror.sh"

ARCH = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine())
pytestmark = pytest.mark.skipif(ARCH is None, reason=f"unpinned arch {platform.machine()}")

# The pins compiled into the script, from the python-build-standalone 20260414
# release SHA256SUMS (recorded 2026-09-29).
OFFICIAL_PIN = {
    "aarch64": "953dcf67ff395d5baff77ef60bc852aaadad4c169e9b8d65dbc40ab9461a13e3",
    "x86_64": "3c3427e5628648478da2aa227472c350475a68bc58109f1b43849636a4aecb89",
}

ARCHIVE = f"cpython-3.12.13+20260414-{ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"


def _fake_release(tmp_path: Path) -> tuple[Path, str]:
    """A download base laid out like the GitHub release: <base>/20260414/<archive>."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"fake-cpython\n"
        info = tarfile.TarInfo("python/bin/python3.12")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    base = tmp_path / "release"
    (base / "20260414").mkdir(parents=True)
    (base / "20260414" / ARCHIVE).write_bytes(buf.getvalue())
    return base, hashlib.sha256(buf.getvalue()).hexdigest()


def _run(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CI_CACHE_ROOT": str(tmp_path / "cache"),
        "GITHUB_ENV": str(tmp_path / "github_env"),
        **env,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)


def _cached(tmp_path: Path) -> Path:
    return tmp_path / "cache/cpython/20260414" / ARCHIVE


def test_miss_downloads_verifies_caches_and_exports_mirror(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "CI_TOOL_CACHE miss tool=cpython version=3.12.13+20260414 source=" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest
    mirror = f"file://{tmp_path}/cache/cpython"
    assert f"UV_PYTHON_INSTALL_MIRROR={mirror}" in (tmp_path / "github_env").read_text()
    assert f"UV_PYTHON_INSTALL_MIRROR={mirror}" in result.stdout


def test_hit_needs_no_network(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    assert _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest).returncode == 0
    (tmp_path / "github_env").unlink()
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE="file:///nonexistent", CPYTHON_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "cache hit" in result.stdout
    assert "CI_TOOL_CACHE miss" not in result.stdout
    assert f"UV_PYTHON_INSTALL_MIRROR=file://{tmp_path}/cache/cpython" in (tmp_path / "github_env").read_text()


def test_corrupted_cache_is_redownloaded_not_reused(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    _cached(tmp_path).parent.mkdir(parents=True)
    _cached(tmp_path).write_bytes(b"half-written")
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "digest mismatch" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest


def test_download_failing_the_pin_is_never_cached(tmp_path: Path) -> None:
    base, _digest = _fake_release(tmp_path)
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256="0" * 64)
    assert result.returncode != 0
    assert "does not match the pinned sha256" in result.stderr
    assert not _cached(tmp_path).exists()
    assert not any(_cached(tmp_path).parent.iterdir())  # no temp file left behind
    assert not (tmp_path / "github_env").exists()  # mirror is exported only on success


def test_without_cache_root_is_a_noop(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}", CPYTHON_ARCHIVE_SHA256=digest, CI_CACHE_ROOT="")
    assert result.returncode == 0, result.stderr
    assert "CI_CACHE_ROOT unset" in result.stdout
    assert not (tmp_path / "cache").exists()
    assert not (tmp_path / "github_env").exists()


def test_builtin_pin_is_selected_for_the_runner_arch(tmp_path: Path) -> None:
    """Without CPYTHON_ARCHIVE_SHA256 the compiled-in per-arch pin is enforced."""

    base, _digest = _fake_release(tmp_path)
    result = _run(tmp_path, CPYTHON_DOWNLOAD_BASE=f"file://{base}")
    assert result.returncode != 0
    assert f"does not match the pinned sha256 {OFFICIAL_PIN[ARCH]}" in result.stderr
