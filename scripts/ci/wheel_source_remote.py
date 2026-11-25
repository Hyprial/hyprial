"""Refuse a wheel whose GUI manifest records a non-public source remote.

Decision (2026-11-16): a published wheel's ``hyprial/_gui/release.json`` must
carry ``sourceRemote`` equal to ``PUBLIC_SOURCE_REMOTE``.  The constant is
loaded by path from the product's ``resources.py`` (scripts cannot import
``src/hyprial``), so the build hook, the snapshot stamp pin test and these
verifiers share one definition.  The load happens outside the project
environment under the publish runner's system python3 (minimum 3.9), so that
file must stay standard-library only.  ``verify_tailcat_wheels.py``,
``publish_wheels.py`` and ``attach_wheel_assets.py`` call this, so a workflow
that forgets ``HYPRIAL_BUILD_SOURCE_REMOTE`` cannot publish or attach an
internal-remote wheel.

Run as a program (``python wheel_source_remote.py <wheel>...``) it exits 0 when
every wheel carries the public remote and 1 with the reason otherwise; the
Developer ID packaging run (``scripts/build/package-macos-shell.sh
--developer-id``) uses it to state in BUILD-INFO.txt whether its artifact is
distributable.
"""

from __future__ import annotations

import json
from pathlib import Path
import runpy
import sys
import zipfile

_RESOURCES = Path(__file__).resolve().parents[2] / "src/hyprial/shell/impl/gui/install/resources.py"
PUBLIC_SOURCE_REMOTE = runpy.run_path(str(_RESOURCES))["PUBLIC_SOURCE_REMOTE"]
MANIFEST_MEMBER = "hyprial/_gui/release.json"
MAX_MANIFEST_BYTES = 1024 * 1024


class SourceRemoteError(ValueError):
    """The wheel does not carry the public source remote."""


def check_archive(archive: zipfile.ZipFile, label: str) -> None:
    names = [info.filename for info in archive.infolist()]
    if names.count(MANIFEST_MEMBER) != 1:
        raise SourceRemoteError(f"{label} must contain exactly one {MANIFEST_MEMBER}")
    declared = archive.getinfo(MANIFEST_MEMBER).file_size
    if declared > MAX_MANIFEST_BYTES:
        raise SourceRemoteError(
            f"{label} {MANIFEST_MEMBER} declares {declared} bytes, above the "
            f"{MAX_MANIFEST_BYTES}-byte limit; refusing to read it"
        )
    try:
        manifest = json.loads(archive.read(MANIFEST_MEMBER).decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise SourceRemoteError(f"{label} {MANIFEST_MEMBER} is not valid JSON: {error}") from error
    remote = manifest.get("sourceRemote") if isinstance(manifest, dict) else None
    if remote != PUBLIC_SOURCE_REMOTE:
        raise SourceRemoteError(
            f"{label} {MANIFEST_MEMBER} sourceRemote is not the public repository "
            f"{PUBLIC_SOURCE_REMOTE}; rebuild with HYPRIAL_BUILD_SOURCE_REMOTE set to it "
            "(a wheel built for local use must not be published)"
        )


def check_wheel(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            check_archive(archive, path.name)
    except zipfile.BadZipFile as error:
        raise SourceRemoteError(f"invalid wheel ZIP {path.name}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        print("usage: wheel_source_remote.py <wheel>...", file=sys.stderr)
        return 2
    for path in paths:
        try:
            check_wheel(Path(path))
        except (SourceRemoteError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
    print(f"public source remote recorded in {len(paths)} wheel(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
