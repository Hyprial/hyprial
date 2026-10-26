"""Read-only native-projection differences for an explicit config rebind.

This is a preview, not a drift waiver. Rebind preserves the old projection and
publishes a new C root; the normal resolver publishes and verifies its new
native projection on the next start.
"""

from pathlib import Path
import stat

from hyprial.kernel import path_is_reparse, private_metadata_violation

from ..home.config import (
    AgentConfig,
    AgentConfigError,
    ConfigManifest,
    _PROJECTION_FILE_MODE,
    build_native_projection,
)
from ..registry._base import Agent


def projection_changes(agent: Agent, home: Path, source: Path) -> list[dict[str, object]]:
    """Describe replacement/omission without reading through linked ancestors."""
    assert agent.config is not None
    staged = AgentConfig(str(source)).freeze_manifest()
    try:
        current = agent.config.freeze_manifest()
    except AgentConfigError:
        # Ordinary rebind also repairs malformed C. Do not prevent that repair
        # merely because the previous projection revision cannot be derived.
        return [{"harness": "*", "path": ".", "changes": ["current-source-invalid"],
                 "action": "publish-from-staging"}]
    changes = []
    for harness in ("claude", "codex", "pi"):
        root = home / "state" / "config" / agent.entity_token / current.revision / "native" / harness
        try:
            if not _present_without_links(home, root):
                continue  # Never published: no incumbent files to replace.
            changes.extend(_harness_changes(harness, root, staged))
        except (AgentConfigError, OSError):
            # The old tree remains untouched. Only validated staging bytes are
            # copied by the saga; an uninspectable incumbent is never trusted.
            changes.append({"harness": harness, "path": ".",
                            "changes": ["uninspectable"], "action": "publish-from-staging"})
    return changes


def _present_without_links(home: Path, root: Path) -> bool:
    current = home
    for part in root.relative_to(home).parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode) or path_is_reparse(metadata):
            raise AgentConfigError("published projection ancestor is not a real directory")
    return True


def _harness_changes(harness: str, root: Path, staged: ConfigManifest) -> list[dict[str, object]]:
    planned = {item.native_path: item for item in build_native_projection(staged, harness).items}
    actual = {item.path: item for item in AgentConfig(str(root)).freeze_manifest().items}
    rows: list[dict[str, object]] = []
    for name in sorted(planned.keys() | actual.keys()):
        expected = planned.get(name)
        observed = actual.get(name)
        reasons = []
        if expected is None:
            reasons.append("unexpected")
        elif observed is None:
            reasons.append("missing")
        elif (expected.digest, expected.size) != (observed.digest, observed.size):
            reasons.append("content")
        if observed is not None:
            path = root / name
            violation = private_metadata_violation(path, path.lstat(), mode=_PROJECTION_FILE_MODE)
            if violation is not None:
                reasons.append("mode" if violation == "unsafe-mode" else "privacy")
        if reasons:
            rows.append({"harness": harness, "path": name, "changes": reasons,
                         "action": "omit-from-new-projection" if expected is None else "publish-from-staging"})
    for directory in (root, *sorted(path for path in root.rglob("*") if path.is_dir())):
        violation = private_metadata_violation(directory, directory.lstat(), mode=0o700)
        if violation is not None:
            reason = "directory-mode" if violation == "unsafe-mode" else "directory-privacy"
            rows.append({"harness": harness, "path": directory.relative_to(root).as_posix(),
                         "changes": [reason], "action": "publish-private-directory"})
    return rows
