"""Bundle the product's GUI at build time, including builds of the public snapshot.

No user configuration, Git history, vendored sources or source tests enter the
wheel. Runtime dependencies remain installed by the existing GUI installer.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile



SOURCE_STAMP = "src/hyprial/gui-source.json"
EXCLUDED = {".agents", ".forgejo", "docs", "tests", "vendor"}


def source_revision(root: Path) -> tuple[str, str]:
    stamp = root / SOURCE_STAMP
    if stamp.is_file():
        import re
        data = json.loads(stamp.read_text())
        if (data.get("schema") != "hyprial.gui-source/v1"
                or not re.fullmatch(r"[a-f0-9]{40}", data.get("commit", ""))
                or not str(data.get("timestamp", "")).isdigit()):
            raise ValueError("Invalid GUI snapshot provenance")
        return data["commit"], str(data["timestamp"])
    return tuple(subprocess.check_output(
        ["git", "-C", str(root), "show", "-s", "--format=%H %ct", "HEAD"], text=True
    ).split())


def stamp_snapshot(root: Path, commit: str, timestamp: str) -> None:
    """Record the exact public tree before it has its own Git history."""
    files = []
    gui = root / "src/gui"
    for path in sorted(gui.rglob("*")):
        if path.is_symlink():
            raise ValueError("GUI snapshots must contain only regular inputs")
        if not path.is_file() or path.relative_to(gui).parts[0] in EXCLUDED:
            continue
        files.append({"path": path.relative_to(gui).as_posix(),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "executable": bool(path.stat().st_mode & 0o111)})
    (root / SOURCE_STAMP).write_text(json.dumps({
        "schema": "hyprial.gui-source/v1", "commit": commit,
        "timestamp": timestamp, "files": files,
    }, indent=2) + "\n")


def build_gui(root: Path, output: Path, version: str, *, product_version: str | None = None) -> None:
    gui = root / "src/gui"
    commit, _ = source_revision(root)
    stamp = root / SOURCE_STAMP
    if stamp.is_file():
        tracked = []
        for row in json.loads(stamp.read_text())["files"]:
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"]:
                raise ValueError("Invalid GUI snapshot path")
            file = gui / relative
            if (file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != row["sha256"]
                    or bool(file.stat().st_mode & 0o111) != row["executable"]):
                raise ValueError("GUI snapshot input changed")
            tracked.append("src/gui/" + relative.as_posix())
    else:
        if subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", "src/gui"]).returncode:
            raise ValueError("Commit GUI changes before building a product release")
        tracked = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "-z", "src/gui"], text=True
        ).split("\0")
    files = {}
    for name in sorted(filter(None, tracked)):
        relative = Path(name).relative_to("src/gui")
        if relative.parts[0] in EXCLUDED:
            continue
        if any(part in {".git", "node_modules", ".ci-cache", "__pycache__"} for part in relative.parts):
            raise ValueError(f"Unexpected GUI build input: {relative}")
        path = gui / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"GUI build requires a regular file: {relative}")
        files[relative.as_posix()] = (path.read_bytes(), bool(path.stat().st_mode & 0o111))
    for required in ("hyprial-install.json", "install.sh", "package.json", "scripts/install-local.sh"):
        if required not in files:
            raise ValueError(f"Missing GUI build input: {required}")
    metadata = {
        "schema": "hyprial.gui-release/v1", "distribution": "product",
        "commit": commit, "version": version,
        "files": [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "executable": executable}
                  for name, (data, executable) in files.items()],
    }
    files["gui-release.json"] = ((json.dumps(metadata, indent=2) + "\n").encode(), False)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, (data, executable) in files.items():
                member = tarfile.TarInfo(f"gui-{version}/{name}")
                member.size = len(data)
                member.mode = 0o755 if executable else 0o644
                archive.addfile(member, io.BytesIO(data))
    payload = buffer.getvalue()
    output.mkdir(parents=True, exist_ok=True)
    (output / "gui.tar.gz").write_bytes(payload)
    (output / "release.json").write_text(json.dumps({
        "schema": "hyprial.product-gui/v1", "version": version,
        "productVersion": product_version or version,
        "commit": commit, "sha256": hashlib.sha256(payload).hexdigest(),
        "file": "gui.tar.gz", "manifest": "hyprial-install.json",
    }, indent=2) + "\n")
