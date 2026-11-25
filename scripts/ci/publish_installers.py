#!/usr/bin/env python3
"""Publish four public installer keys, or check drift without R2 credentials."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

from r2_upload import Object, PUBLIC_BASE, R2, publish_objects, signed_put, verify_public

__all__ = ["publish", "check_drift", "installer_objects", "INSTALLER_KEYS", "signed_put"]
INSTALLER_KEYS = frozenset({"install.sh", "install.sh.sha256", "install.ps1", "install.ps1.sha256"})


def installer_objects(root: Path) -> list[Object]:
    objects = []
    for name, content_type in (("install.sh", "text/x-shellscript"), ("install.ps1", "text/plain")):
        body = (root / name).read_bytes()
        if not body:
            raise RuntimeError(f"Empty installer: {name}")
        checksum = f"{hashlib.sha256(body).hexdigest()}  {name}\n".encode("ascii")
        objects.extend((Object(name, body, content_type), Object(name + ".sha256", checksum, "text/plain")))
    return objects


def publish(root: Path, env: dict[str, str], *, endpoint: str | None = None,
            public_base: str = PUBLIC_BASE) -> None:
    client = R2(env, endpoint=endpoint)
    publish_objects(client, installer_objects(root), INSTALLER_KEYS.__contains__, public_base=public_base)


def check_drift(root: Path, *, public_base: str = PUBLIC_BASE) -> None:
    for obj in installer_objects(root):
        try:
            verify_public(obj, public_base)
        except RuntimeError as error:
            raise RuntimeError(f"dl out of sync with repo: {obj.key}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Anonymous comparison only; no R2 secrets needed")
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        if args.check:
            check_drift(root)
        else:
            publish(root, dict(os.environ))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Installer publication/check failed (local file or transport error)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
