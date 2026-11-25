'scripts/ci/ci-ensure-go.sh: cache-first, sha256-pinned Go toolchain for CI.\n\nSame #941 semantics as tests/tooling/ci/acquisition/test_ci_ensure_uv.py: the cached tarball is\ntrusted only when its digest equals the pin, so a half-written or corrupted\nfile on the shared cache volume is re-downloaded, never reused; a download\nthat fails the pin is never cached; mirrors are tried in order.  The official\nper-arch pins are compiled into the script and selected by `uname -m`.  Runs\noffline against a fake toolchain archive served over file://.\n'

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
SCRIPT = ROOT / "scripts/ci/ci-ensure-go.sh"

ARCH = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(platform.machine())
pytestmark = pytest.mark.skipif(ARCH is None, reason=f"unpinned arch {platform.machine()}")

# The pins compiled into the script, from https://go.dev/dl/?mode=json&include=all
# (release go1.26.8, recorded 2026-09-29).
OFFICIAL_PIN = {
    "amd64": "d0f743b33e8d8945e6b1f432edd15785c70507121d6e2a723b21285eddf8b57b",
    "arm64": "211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0",
}

ARCHIVE = f"go1.26.8.linux-{ARCH}.tar.gz"


def _fake_release(
    tmp_path: Path, *, version: str = "1.26.8", arch: str = ARCH, go_os: str = "linux"
) -> tuple[Path, str]:
    """A mirror dir laid out like the official bases: <base>/go1.26.8.linux-<arch>.tar.gz."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = f'#!/bin/sh\necho "go version go{version} linux/{arch} (fake)"\n'.encode()
        info = tarfile.TarInfo("go/bin/go")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    base = tmp_path / "mirror"
    base.mkdir()
    (base / f"go{version}.{go_os}-{arch}.tar.gz").write_bytes(buf.getvalue())
    return base, hashlib.sha256(buf.getvalue()).hexdigest()


def _run(tmp_path: Path, name: str, **env: str) -> subprocess.CompletedProcess[str]:
    commands = tmp_path / "default-commands"
    commands.mkdir(exist_ok=True)
    uname = commands / "uname"
    uname.write_text(f"#!/bin/sh\nif [ \"$1\" = -s ]; then echo Linux; else echo {ARCH}; fi\n")
    uname.chmod(0o755)
    full = {
        "PATH": f"{commands}{os.pathsep}{os.environ["PATH"]}",
        "HOME": str(tmp_path),
        "CI_CACHE_ROOT": str(tmp_path / "cache"),
        "GO_ROOT_DIR": str(tmp_path / f"root-{name}"),
        "GITHUB_PATH": str(tmp_path / "github_path"),
        **env,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)


def _cached(tmp_path: Path) -> Path:
    return tmp_path / "cache/go/1.26.8" / ARCHIVE


def test_miss_downloads_verifies_and_caches(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    result = _run(tmp_path, "a", GO_DOWNLOAD_BASES=f"file://{base}", GO_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "go version go1.26.8" in result.stdout
    assert "CI_TOOL_CACHE miss tool=go version=1.26.8 source=" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest
    assert str(tmp_path / "root-a/go/bin") in (tmp_path / "github_path").read_text()


def test_hit_needs_no_network(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    assert _run(tmp_path, "a", GO_DOWNLOAD_BASES=f"file://{base}", GO_ARCHIVE_SHA256=digest).returncode == 0
    result = _run(tmp_path, "b", GO_DOWNLOAD_BASES="file:///nonexistent", GO_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "cache hit" in result.stdout
    assert "CI_TOOL_CACHE miss" not in result.stdout


def test_corrupted_cache_is_redownloaded_not_reused(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    _cached(tmp_path).parent.mkdir(parents=True)
    _cached(tmp_path).write_bytes(b"half-written")
    result = _run(tmp_path, "c", GO_DOWNLOAD_BASES=f"file://{base}", GO_ARCHIVE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert "digest mismatch" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest


def test_first_mirror_fails_second_succeeds(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    result = _run(
        tmp_path,
        "m",
        GO_DOWNLOAD_BASES=f"file:///nonexistent file://{base}",
        GO_ARCHIVE_SHA256=digest,
    )
    assert result.returncode == 0, result.stderr
    assert "trying next mirror" in result.stderr
    assert f"(from file://{base}/{ARCHIVE})" in result.stdout
    assert hashlib.sha256(_cached(tmp_path).read_bytes()).hexdigest() == digest


def test_download_failing_the_pin_is_never_cached(tmp_path: Path) -> None:
    base, _digest = _fake_release(tmp_path)
    result = _run(tmp_path, "d", GO_DOWNLOAD_BASES=f"file://{base}", GO_ARCHIVE_SHA256="0" * 64)
    assert result.returncode != 0
    assert "does not match the pinned sha256" in result.stderr
    assert not _cached(tmp_path).exists()
    assert not any(_cached(tmp_path).parent.iterdir())  # no temp file left behind


def test_without_cache_root_still_verifies_the_pin(tmp_path: Path) -> None:
    base, digest = _fake_release(tmp_path)
    env = {"GO_DOWNLOAD_BASES": f"file://{base}", "GO_ARCHIVE_SHA256": digest}
    good = _run(tmp_path, "n", **env, CI_CACHE_ROOT="")
    assert good.returncode == 0, good.stderr
    assert "CI_CACHE_ROOT unset" in good.stdout
    assert "go version go1.26.8" in good.stdout
    assert not (tmp_path / "cache").exists()
    bad = _run(tmp_path, "n2", GO_DOWNLOAD_BASES=f"file://{base}", GO_ARCHIVE_SHA256="0" * 64, CI_CACHE_ROOT="")
    assert bad.returncode != 0
    assert "does not match the pinned sha256" in bad.stderr


def test_builtin_pin_is_selected_for_the_runner_arch(tmp_path: Path) -> None:
    """Without GO_ARCHIVE_SHA256 the compiled-in per-arch pin is enforced."""

    base, _digest = _fake_release(tmp_path)
    result = _run(tmp_path, "p", GO_DOWNLOAD_BASES=f"file://{base}")
    assert result.returncode != 0
    assert f"does not match the pinned sha256 {OFFICIAL_PIN[ARCH]}" in result.stderr


@pytest.mark.parametrize("go_os,arch,pin", [
    ("linux", "amd64", "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445"),
    ("linux", "arm64", "3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec"),
    ("darwin", "arm64", "ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12"),
])
def test_tailcat_go127_uses_official_pin_and_rejects_fake_archive(
    tmp_path: Path, go_os: str, arch: str, pin: str
) -> None:
    """Exercise both runner branches offline; never install the fake bytes."""
    # Published archive digests from go.dev's official JSON, 2026-10-03.
    base, _digest = _fake_release(tmp_path, version="1.27.1", arch=arch, go_os=go_os)
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text(f"#!/bin/sh\nif [ \"$1\" = -s ]; then echo {go_os.title()}; else echo {arch}; fi\n")
    uname.chmod(0o755)
    result = _run(
        tmp_path, "tailcat", GO_VERSION="1.27.1",
        PATH=f"{commands}{os.pathsep}{os.environ['PATH']}",
        GO_DOWNLOAD_BASES=f"file://{base}",
    )
    assert result.returncode != 0
    assert f"does not match the pinned sha256 {pin}" in result.stderr
    assert "no pinned digest" not in result.stderr
    cached = tmp_path / "cache/go/1.27.1"
    assert cached.is_dir() and not any(cached.iterdir())
    assert not (tmp_path / "root-tailcat/go").exists()
