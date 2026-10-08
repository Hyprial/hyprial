"""Bundle the independent GUI, including builds of the public snapshot.

No user configuration, Git history, vendored sources or source tests enter the
wheel. The builtin GUI unpacker installs the locked runtime dependencies.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from os import name as os_name
from pathlib import Path
import subprocess
import tarfile



SOURCE_STAMP = "src/hyprial/gui-source.json"
EXCLUDED = {".agents", ".forgejo", "docs", "tests", "vendor"}
# Exact first-party composition closure. Dependencies remain locked by npm;
# the explicit mock fixture data is not a required production backend.
PRODUCT_INPUTS = (
    "product/index.mjs", "product/server.mjs", "product/server-core.mjs",
    "product/config.mjs", "product/page.mjs", "product/build.mjs",
    "product/assets/client.js", "product/assets/style.css",
    "client/index.mjs", "client/composer.mjs", "client/workspace.mjs", "client/ui-runtime.mjs",
    "session/index.mjs", "studio/index.mjs", "studio/core.mjs",
    "studio/reference-authoring.mjs", "studio/memory-persistence.mjs", "studio/fs-persistence.mjs",
    "host/index.mjs", "presentation/index.mjs", "presentation/theme.mjs",
    "transport/index.mjs", "plugins/trusted-panel.mjs",
)
REQUIRED_INPUTS = (
    "hyprial-install.json", "install.sh", "package.json", "package-lock.json",
    "scripts/start-gui.sh", *PRODUCT_INPUTS, "legal/LICENSE",
)


def _git_executable_modes(root: Path) -> dict[str, bool]:
    """Read canonical regular-file modes, including on Windows filesystems."""
    result = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--stage", "-z", "--", "src/gui"]
    )
    modes = {}
    for record in filter(None, result.split(b"\0")):
        metadata, name = record.split(b"\t", 1)
        mode, _object_id, stage = metadata.split()
        if stage != b"0" or mode not in {b"100644", b"100755"}:
            raise ValueError("GUI build requires unconflicted regular Git inputs")
        path = name.decode("utf-8", "surrogateescape")
        if path in modes:
            raise ValueError("Duplicate GUI Git input")
        modes[path] = mode == b"100755"
    return modes


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
    # Windows stat modes do not preserve POSIX executable bits. A checkout
    # has independent index metadata; an exported snapshot instead carries
    # its canonical modes in the same provenance manifest as its byte hashes.
    # Do not discover an unrelated parent checkout for a source archive.
    index_modes = (
        _git_executable_modes(root)
        if os_name == "nt" and ((root / ".git").exists() or not stamp.is_file())
        else None
    )
    snapshot_modes = {}
    if stamp.is_file():
        tracked = []
        for row in json.loads(stamp.read_text())["files"]:
            relative = Path(row["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"]:
                raise ValueError("Invalid GUI snapshot path")
            if type(row["executable"]) is not bool:
                raise ValueError("Invalid GUI snapshot executable mode")
            file = gui / relative
            name = "src/gui/" + relative.as_posix()
            executable = (
                index_modes.get(name) if index_modes is not None
                else row["executable"] if os_name == "nt"
                else bool(file.stat().st_mode & 0o111)
            )
            if (file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != row["sha256"]
                    or executable != row["executable"]):
                raise ValueError("GUI snapshot input changed")
            snapshot_modes[name] = row["executable"]
            tracked.append(name)
    else:
        dirty = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", "src/gui"]).returncode
        untracked = subprocess.check_output(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z", "--", "src/gui"]
        )
        if dirty or untracked:
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
        executable = (
            snapshot_modes[name] if stamp.is_file()
            else index_modes[name] if index_modes is not None
            else bool(path.stat().st_mode & 0o111)
        )
        files[relative.as_posix()] = (path.read_bytes(), executable)
    for required in REQUIRED_INPUTS:
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
