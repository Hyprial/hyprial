"""Identity construction stays behind the canonical kernel helpers."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPO_ROOT / "src" / "hyprial"

# These calls record the physical host in lifecycle diagnostics or as a
# human-facing display default.  They do not choose the daemon's durable node
# identity.
HOST_DIAGNOSTIC_ALLOWLIST = {
    # Death-path backstop arming moves this existing host diagnostic by one
    # line; the call and its host-only purpose are unchanged.
    "daemon/impl/application/startup.py:883": "daemon startup failure host field",
    # GUI-FRESHESTART-20261027 cli-dev-entry added two comment lines above the
    # same reviewed upgrade calls; the shared group-factory import adds one
    # more line. The call sites themselves are unchanged.
    "shell/impl/cli/commands/upgrade/commands.py:74": "upgrade process host snapshot",
    # F5: the restart-path host fields consolidated into the shared skeleton
    # (daemon/restart.py's two sites retired; line numbers shifted by the
    # skeleton's insertion). #1913's verdict changes and GUI retirement
    # moved these same four diagnostic calls; no new hostname construction
    # is permitted. Pin the exact merged call sites from both changes.
    "shell/impl/cli/commands/common/pending_restart.py:305": "survivor marker host field (shared restart skeleton)",
    "shell/impl/cli/commands/common/pending_restart.py:314": "survivor alert host field (shared restart skeleton)",
    "shell/impl/cli/commands/common/pending_restart.py:420": "restart-failure host snapshot (shared restart skeleton, upgrade path)",
    "shell/impl/cli/commands/upgrade/commands.py:656": "upgrade handoff host snapshot",
    # #1607/d6b7a0db4 moved this existing hostname site 102 -> 107, and #1852
    # (F6 machine binding) moved it again; it is the same call.
    "daemon/impl/network/tailcat/keys.py:268": (
        "device display-name default only; device identity is deviceUid (P1)"
    ),
    # #1852 F6: last-resort machine fingerprint input (hashed with the MAC and
    # os.name) when no OS machine id is readable; never a node or device id.
    "identity/impl/device/machine_binding.py:178": (
        "machine-binding fingerprint fallback input only (hashed; not an identity)"
    ),
}

# Compatibility-only construction from pre-validation stored/migration data.
# Each call preserves the former f"user:{value}" output; new identity writers
# must use canonical_user_uri instead.
LEGACY_USER_URI_ALLOWLIST = {
    "daemon/impl/application/actors/routines.py:209": "stored agent owner in routine escalation",
    "daemon/impl/application/actors/routines.py:223": "stored agent owner in routine record",
    "daemon/impl/application/actors/routines.py:232": "stored agent owner in coordinator request",
    "daemon/impl/application/messaging/status/views.py:821": "stored profile owner in target listing",
    "daemon/impl/owner_migration/planning.py:77": "match pre-validation owner address",
    "daemon/impl/owner_migration/planning.py:78": "rewrite pre-validation owner address",
    "daemon/impl/pac/storage/migrations/upgrades.py:248": "upgrade legacy short owner",
}


def _source_lines() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        rows.extend(
            (relative, number, line)
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            )
        )
    return rows


def test_gethostname_is_only_node_helper_or_justified_host_diagnostic() -> None:
    observed = {
        f"{path}:{number}"
        for path, number, line in _source_lines()
        if "gethostname(" in line
    }
    assert observed == set(HOST_DIAGNOSTIC_ALLOWLIST), (
        "socket.gethostname() outside the canonical node-id helper must be a "
        "reviewed host-only diagnostic; update the shrinking allowlist with a "
        f"reason when genuine: {sorted(observed ^ set(HOST_DIAGNOSTIC_ALLOWLIST))}"
    )


def test_no_hand_built_f_string_user_uri() -> None:
    findings = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in _source_lines()
        if 'f"user:' in line or "f'user:" in line
    ]
    assert not findings, "use canonical_user_uri():\n" + "\n".join(findings)


def test_legacy_user_uri_calls_are_exactly_the_reviewed_compatibility_sites() -> None:
    observed: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        observed.update(
            f"{relative}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "legacy_user_uri"
        )
    assert observed == set(LEGACY_USER_URI_ALLOWLIST), (
        "legacy_user_uri() is only for reviewed stored/migration compatibility; "
        f"unexpected allowlist delta: {sorted(observed ^ set(LEGACY_USER_URI_ALLOWLIST))}"
    )
