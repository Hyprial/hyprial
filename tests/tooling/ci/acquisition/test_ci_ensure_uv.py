"""scripts/ci/ci-ensure-uv.sh: cache-first, sha256-pinned uv for CI jobs.

The cached release archive is trusted only when its digest equals the pin, so a
half-written or corrupted file on the shared cache volume is re-downloaded,
never reused; a download that fails the pin is never cached.  Runs offline
against a fake archive served over file://.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/ci-ensure-uv.sh"

ARCH = {"arm64": "aarch64", "aarch64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine())
pytestmark = pytest.mark.skipif(ARCH is None, reason=f"unpinned arch {platform.machine()}")


def test_default_downloads_use_uv_custom_mirror() -> None:
    text = SCRIPT.read_text()
    mirror = "https://uv.agentsmirror.com/github/astral-sh/uv/releases/download"
    assert mirror in text
    assert '${UV_VERSION}/uv-installer.sh' in text
    assert "https://astral.sh/uv/" not in text


def _fake_release(tmp_path: Path) -> tuple[Path, str]:
    """A release dir laid out like GitHub's: <base>/0.11.7/uv-<arch>-unknown-linux-gnu.tar.gz."""

    archive_name = f"uv-{ARCH}-unknown-linux-gnu.tar.gz"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b'#!/bin/sh\necho "uv 0.11.7 (fake)"\n'
        info = tarfile.TarInfo(f"uv-{ARCH}-unknown-linux-gnu/uv")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    base = tmp_path / "release"
    (base / "0.11.7").mkdir(parents=True)
    (base / "0.11.7" / archive_name).write_bytes(buf.getvalue())
    return base, hashlib.sha256(buf.getvalue()).hexdigest()


def _run(tmp_path: Path, name: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "CI_CACHE_ROOT": str(tmp_path / "cache"),
        "UV_BIN_DIR": str(tmp_path / f"bin-{name}"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
        **env,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)


def _cached(tmp_path: Path) -> Path:
    return tmp_path / "cache/uv/0.11.7" / f"uv-{ARCH}-unknown-linux-gnu.tar.gz"


def test_miss_downloads_verifies_and_caches(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    result = _run(tmp_path, "a", UV_DOWNLOAD_BASE=f"file://{base}", UV_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "uv 0.11.7" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest
    assert str(tmp_path / "bin-a") in (tmp_path / "github_path").read_text()


def test_hit_needs_no_network(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    assert _run(tmp_path, "a", UV_DOWNLOAD_BASE=f"file://{base}", UV_ARCHIVE_SHA256=digest).returncode == 0
    result = _run(tmp_path, "b", UV_DOWNLOAD_BASE="file:///nonexistent", UV_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "cache hit" in result.stdout


def test_corrupted_cache_is_redownloaded_not_reused(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    _cached(tmp_path).parent.mkdir(parents=True)
    _cached(tmp_path).write_bytes(b"half-written")
    result = _run(tmp_path, "c", UV_DOWNLOAD_BASE=f"file://{base}", UV_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "digest mismatch" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest


def test_download_failing_the_pin_is_never_cached(tmp_path: Path) -> None:
    base, _digest = _fake_release(tmp_path)
    result = _run(tmp_path, "d", UV_DOWNLOAD_BASE=f"file://{base}", UV_ARCHIVE_SHA256="0" * 64)
    assert result.returncode != 0
    assert "does not match the pinned sha256" in result.stderr
    assert not _cached(tmp_path).exists()
    assert not any(_cached(tmp_path).parent.iterdir())  # no temp file left behind


def test_unpinned_version_fails_instead_of_falling_back(tmp_path: Path) -> None:
    """With the cache in play, a version bump that forgot its pin must fail loudly
    rather than silently switch to an unverified install.sh."""

    result = _run(tmp_path, "e", UV_VERSION="9.9.9", UV_DOWNLOAD_BASE="file:///nonexistent")
    assert result.returncode != 0
    assert "no pinned digest for 9.9.9/" in result.stderr
    assert "install.sh" not in result.stdout


def test_day_old_stray_temp_files_are_pruned(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    cache_dir = _cached(tmp_path).parent
    cache_dir.mkdir(parents=True)
    old = cache_dir / f".{_cached(tmp_path).name}.OLD123"
    fresh = cache_dir / f".{_cached(tmp_path).name}.NEW456"
    old.write_bytes(b"partial")
    fresh.write_bytes(b"partial")
    two_days_ago = time.time() - 2 * 86400
    os.utime(old, (two_days_ago, two_days_ago))
    result = _run(tmp_path, "f", UV_DOWNLOAD_BASE=f"file://{base}", UV_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert fresh.exists()  # a concurrent job's in-flight download is left alone
