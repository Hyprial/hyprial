#!/usr/bin/env python3
"""Publish one complete five-platform wheel set directly to R2; SUMS last."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import zipfile

from r2_upload import Object, PUBLIC_BASE, R2, publish_objects

_SOURCE_REMOTE = runpy.run_path(str(Path(__file__).with_name("wheel_source_remote.py")))
TRACKS = frozenset({"stable", "nightly", "internal"})
PLATFORMS = frozenset({
    "macosx_13_0_arm64", "macosx_13_0_x86_64",
    "manylinux_2_17_x86_64.manylinux2014_x86_64",
    "manylinux_2_17_aarch64.manylinux2014_aarch64", "win_amd64",
})
WHEEL = re.compile(r"hyprial-([0-9][A-Za-z0-9.!+_]*)-(py3-none|cp312-abi3)-([A-Za-z0-9_.]+)\.whl")


def wheel_objects(directory: Path, track: str) -> list[Object]:
    if track not in TRACKS:
        raise RuntimeError("Invalid wheel track: use stable, nightly or internal")
    try:
        manifest = (directory / "SHA256SUMS").read_bytes()
        lines = manifest.decode("ascii").splitlines()
        if len(lines) != len(PLATFORMS):
            raise ValueError("expected five entries")
        objects = []
        platforms = set()
        versions = set()
        abis = set()
        for line in lines:
            entry = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)", line)
            if entry is None:
                raise ValueError("malformed checksum entry")
            digest, name = entry.groups()
            wheel = WHEEL.fullmatch(name)
            if wheel is None:
                raise ValueError("invalid wheel basename")
            version, abi, platform = wheel.groups()
            if platform not in PLATFORMS or platform in platforms:
                raise ValueError("unexpected or duplicate platform")
            platforms.add(platform)
            versions.add(version)
            abis.add(abi)
            body = (directory / name).read_bytes()
            if hashlib.sha256(body).hexdigest() != digest.lower():
                raise ValueError("wheel checksum mismatch")
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    _SOURCE_REMOTE["check_archive"](archive, name)
            except zipfile.BadZipFile as error:
                raise ValueError("wheel is not a ZIP archive") from error
            except _SOURCE_REMOTE["SourceRemoteError"] as error:
                raise RuntimeError(f"Refusing to publish: {error}") from error
            objects.append(Object(f"wheel/{track}/{name}", body, "application/octet-stream"))
        if platforms != PLATFORMS or len(versions) != 1 or len(abis) != 1:
            raise ValueError("incomplete set or mixed versions/ABIs")
    except (OSError, UnicodeError, ValueError) as error:
        raise RuntimeError("Invalid wheel set: five same-version platform wheels with valid SHA256SUMS required") from error
    objects.append(Object(f"wheel/{track}/SHA256SUMS", manifest, "text/plain"))
    return objects


def require_current_track(track: str, source_sha: str) -> None:
    """Read origin, not local tags; annotated tags must resolve to their commit."""
    if track not in TRACKS or not re.fullmatch(r"[0-9a-fA-F]{40}", source_sha):
        raise RuntimeError("Track freshness requires a valid track and full source SHA")
    ref = f"refs/tags/{track}"
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", ref, ref + "^{}"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"Cannot resolve source-remote track: {track}") from None
    if result.returncode == 2:
        raise RuntimeError(f"stale run: {track} now at missing")
    if result.returncode:
        # Git diagnostics may contain URLs or auth details; never forward them.
        raise RuntimeError(f"Cannot resolve source-remote track: {track}")
    refs = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if (len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{40}", fields[0])
                or fields[1] not in (ref, ref + "^{}") or fields[1] in refs):
            raise RuntimeError(f"Cannot resolve source-remote track: {track} (invalid response)")
        refs[fields[1]] = fields[0].lower()
    current = refs.get(ref + "^{}", refs.get(ref, "missing"))
    if ref not in refs or current != source_sha.lower():
        raise RuntimeError(f"stale run: {track} now at {current}")


def publish(directory: Path, track: str, env: dict[str, str], *, source_sha: str,
            endpoint: str | None = None, public_base: str = PUBLIC_BASE) -> None:
    objects = wheel_objects(directory, track)  # Refuse incomplete sets before network I/O.
    require_current_track(track, source_sha)  # Before ANY bucket upload.
    client = R2(env, endpoint=endpoint)
    prefix = f"wheel/{track}/"

    def check_manifest(obj: Object) -> None:
        if obj.key == prefix + "SHA256SUMS":
            require_current_track(track, source_sha)

    publish_objects(client, objects, lambda key: key.startswith(prefix),
                    public_base=public_base, verify_each=True, before_put=check_manifest,
                    # 守卫只看本 track 的前缀: 快照级收敛,而不是把别人的写入判成我方越界。
                    snapshot_prefix=prefix)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--track", required=True, choices=sorted(TRACKS))
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    parser.add_argument("--source-sha", required=True, help="Full immutable commit used to build this set")
    args = parser.parse_args()
    try:
        publish(args.directory, args.track, dict(os.environ), source_sha=args.source_sha)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Wheel publication failed (local file or transport error)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
