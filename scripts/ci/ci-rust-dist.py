#!/usr/bin/env python3
"""Prefetch only official, fully verified Rust archives into isolated homes.

Rustup 1.28.2 consumes complete downloads/<sha256> files; a real installation
with all archive HTTP requests refused verifies this interface in the evidence.
Partial downloads stay private to the current run. Cache publication is atomic.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit

VERSION = "1.93.1"
HOSTS = {
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu",
    "aarch64-apple-darwin", "x86_64-apple-darwin",
}
COMPONENTS = ("cargo", "rust-std", "rustc")
MANIFEST_URL = f"https://static.rust-lang.org/dist/channel-rust-{VERSION}.toml"


class ProvisioningError(ValueError):
    """Do not install from an incomplete, unsafe or unverified input."""


def official_url(value: str) -> None:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https" and parsed.netloc == "static.rust-lang.org"
            and parsed.hostname == "static.rust-lang.org" and parsed.port is None
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment and parsed.path.startswith("/dist/")
            and re.fullmatch(r"[A-Za-z0-9_./\-]+", parsed.path) is not None
            and ".." not in parsed.path.split("/")
        )
    except ValueError:
        valid = False
    if not valid:
        raise ProvisioningError("non-official Rust distribution URL refused")


def select_components(data: bytes, host: str, version: str) -> list[dict[str, str]]:
    if version != VERSION or host not in HOSTS:
        raise ProvisioningError("unsupported Rust version or host")
    try:
        value = tomllib.loads(data.decode("utf-8"))
        date = value["date"]
        if value["manifest-version"] != "2" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ProvisioningError("invalid official Rust manifest header")
        packages = value["pkg"]
        applicable = set()
        for name in value["profiles"]["minimal"]:
            targets = packages[name]["target"]
            target = targets.get(host, targets.get("*", {}))
            if target.get("available") is True:
                applicable.add(name)
        if applicable != set(COMPONENTS):
            raise ProvisioningError("unexpected same-host minimal toolchain component set")
        for name in ("rust", "rust-std", "rustc"):
            if not packages[name]["version"].startswith(VERSION + " ("):
                raise ProvisioningError("official manifest has the wrong Rust version")
        result = []
        for name in COMPONENTS:
            target = packages[name]["target"][host]
            url, digest = target["xz_url"], target["xz_hash"]
            official_url(url)
            expected = f"https://static.rust-lang.org/dist/{date}/{name}-{VERSION}-{host}.tar.xz"
            if target["available"] is not True or url != expected or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ProvisioningError(f"{name}: unavailable, wrong host/version URL or invalid checksum")
            result.append({"component": name, "url": url, "sha256": digest})
        return result
    except (KeyError, TypeError, AttributeError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ProvisioningError("incomplete or malformed official Rust manifest") from error


def directory(path: Path, *, boundary: Path | None = None) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or "\n" in str(path) or "\r" in str(path):
        raise ProvisioningError("Rust dist directories must be absolute canonical paths")
    home_text = os.environ.get("HOME")
    if not home_text:
        raise ProvisioningError("HOME is required for isolation validation")
    home = Path(home_text).resolve()
    forbidden = (home / ".cargo", home / ".rustup", home / ".hyprial")
    if path in (Path("/"), home) or any(path == item or path.is_relative_to(item) for item in forbidden):
        raise ProvisioningError("Rust dist path must not use a global user home")
    if boundary is not None and (path == boundary or not path.is_relative_to(boundary)):
        raise ProvisioningError("Rust dist downloads must stay inside this E2E run")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ProvisioningError("Rust dist directory symlink refused")
        if candidate.exists() and not candidate.is_dir():
            raise ProvisioningError("Rust dist directory is not a directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def checksum(path: Path, expected: str) -> None:
    try:
        if not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink():
            raise ProvisioningError("Rust archive must be a regular non-symlink file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or path.is_symlink():
                raise ProvisioningError("Rust archive must be a regular non-symlink file")
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as error:
        raise ProvisioningError("Rust archive must be a readable regular non-symlink file") from error
    if digest != expected:
        raise ProvisioningError("Rust archive checksum mismatch; refusing reuse/publication")


def publish_copy(source: Path, destination: Path, expected: str) -> None:
    checksum(source, expected)
    if destination.exists() or destination.is_symlink():
        checksum(destination, expected)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=destination.parent)
    pending = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        checksum(pending, expected)
        if destination.exists() or destination.is_symlink():
            checksum(destination, expected)
        else:
            os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)


def download(url: str, destination: Path, component: str) -> None:
    official_url(url)
    small = component in {"manifest", "manifest-sha"}
    print(f"e2e-rust-dist: download component={component} url={url}", flush=True)
    command = [
        "curl", "--fail", "--location", "--max-redirs", "0", "--proto", "=https",
        "--proto-redir", "=https", "--silent", "--show-error", "--connect-timeout", "10",
        "--max-time", "60" if small else "240", "--speed-time", "30",
        "--speed-limit", "1024" if small and platform.system() == "Darwin" else "16384",
        "--retry", "1" if small else "2", "--retry-delay", "2", "--retry-max-time", "90" if small else "600",
        "--write-out", f"e2e-rust-dist: transfer component={component} http=%{{http_code}} bytes=%{{size_download}} seconds=%{{time_total}}\n",
        "--output", str(destination), url,
    ]
    environment = {key: value for key, value in os.environ.items() if not key.endswith(("_TOKEN", "_SECRET"))}
    try:
        result = subprocess.run(command, env=environment, timeout=140 if small else 750)
    except subprocess.TimeoutExpired as error:
        raise ProvisioningError(f"{component}: bounded curl process deadline exceeded") from error
    if result.returncode:
        raise ProvisioningError(f"{component}: official download failed (curl exit {result.returncode})")


def prepare_dist(host: str, version: str, root: Path, cache: Path, downloads: Path) -> list[dict[str, str]]:
    if version != VERSION or host not in HOSTS:
        raise ProvisioningError("unsupported Rust version or host")
    root = directory(root)
    downloads = directory(downloads, boundary=root)
    cache = directory(cache)
    if cache == downloads.parent or cache.is_relative_to(downloads.parent):
        raise ProvisioningError("archive cache must not share the run's installation home")
    archives = directory(cache / "dist" / VERSION / host)
    with tempfile.TemporaryDirectory(prefix=".e2e-dist-", dir=downloads) as temporary:
        scratch = Path(temporary)
        manifest = scratch / "manifest.toml"
        official_sha = scratch / "manifest.sha256"
        download(MANIFEST_URL, manifest, "manifest")
        download(MANIFEST_URL + ".sha256", official_sha, "manifest-sha")
        matched = re.fullmatch(r"([0-9a-f]{64})(?:\s+\*?channel-rust-1\.93\.1\.toml)?\s*", official_sha.read_text())
        if not matched:
            raise ProvisioningError("invalid official manifest checksum response")
        checksum(manifest, matched[1])
        selected = select_components(manifest.read_bytes(), host, version)
        for item in selected:
            name, digest = item["component"], item["sha256"]
            cached = archives / digest
            private = downloads / digest
            if private.exists() or private.is_symlink():
                checksum(private, digest)
            if cached.exists() or cached.is_symlink():
                checksum(cached, digest)
                print(f"CI_TOOL_CACHE hit tool=rust-dist version={VERSION} host={host} component={name} sha256={digest}", flush=True)
            else:
                print(f"CI_TOOL_CACHE miss tool=rust-dist version={VERSION} host={host} component={name}", flush=True)
                fetched = scratch / f"{name}.tar.xz"
                download(item["url"], fetched, name)
                checksum(fetched, digest)
                publish_copy(fetched, cached, digest)
            publish_copy(cached, private, digest)
            print(f"e2e-rust-dist: staged component={name} sha256={digest}", flush=True)
    return selected


def current_host() -> str:
    key = (platform.system(), platform.machine().lower())
    return {
        ("Linux", "x86_64"): "x86_64-unknown-linux-gnu", ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
        ("Linux", "amd64"): "x86_64-unknown-linux-gnu",
        ("Linux", "arm64"): "aarch64-unknown-linux-gnu", ("Darwin", "arm64"): "aarch64-apple-darwin",
        ("Darwin", "aarch64"): "aarch64-apple-darwin", ("Darwin", "x86_64"): "x86_64-apple-darwin",
    }.get(key, "unsupported")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--downloads", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.host != current_host():
            raise ProvisioningError("requested Rust distribution host does not match this machine")
        prepare_dist(args.host, args.version, args.run_root, args.cache_root, args.downloads)
    except (ProvisioningError, OSError) as error:
        print(f"e2e-rust-dist: FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
