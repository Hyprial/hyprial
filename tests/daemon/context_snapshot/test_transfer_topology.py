from __future__ import annotations

import json
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle, validate_bundle
from hyprial.daemon.impl.transfer.cleanup import execute_cleanup, plan_cleanup
from hyprial.daemon.impl.transfer.landing.registry import (
    CUSTODY_RECEIPT,
    HOME_TOPOLOGY,
    land_bundle_into_registry,
)
from hyprial.identity import AgentRegistry
from hyprial.identity.impl.agents.home.provisioner import _CLEANUP_DIRECTORIES
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "source-home"
    for name in ("state", "config", "secrets"):
        (home / name).mkdir(parents=True)
    (home / CUSTODY_RECEIPT).write_text(
        json.dumps({"actor": "worker", "path": "/source/worker", "resource": "x"})
    )
    handoff = home / "state" / "context" / "handoff" / "switch-1"
    handoff.mkdir(parents=True)
    (handoff / "manifest.json").write_text('{"schemaVersion":1}\n')
    (handoff / "handoff.md").write_text("# handoff\n")
    return home


def test_the_landing_topology_is_the_cleanup_topology() -> None:
    """One invariant, two owners: a landing may admit exactly what cleanup covers.

    ``registry.HOME_TOPOLOGY`` decides which top-level entries a landing may
    create; ``provisioner._CLEANUP_DIRECTORIES`` decides which ones the home
    fences can remove.  They are maintained in separate layers by separate
    authors, and nothing tied them together -- which is how the two-machine
    reading of 2026-10-12 got a green dry run for a home the registry cannot
    clean up.  Pinned as a pair, because the invariant *is* the pair: either
    side drifting alone re-opens that false green.
    """

    assert set(HOME_TOPOLOGY) == {item.name for item in _CLEANUP_DIRECTORIES}


def test_state_handoff_subtree_transfers_lands_and_remains_cleanup_covered(
    tmp_path: Path,
) -> None:
    # Handoff is legal only because it remains below state/. Relaxing the
    # existing top-level closure would make this phase-1 proof meaningless.
    assert HOME_TOPOLOGY == ("state", "config", "secrets", "workspace")
    source = _source_home(tmp_path)
    bundle = tmp_path / "bundle"
    export_bundle(
        source,
        bundle,
        owner="owner",
        actor="worker",
        source_machine="source-node",
        created_at="2026-10-09T00:00:00+08:00",
    )
    manifest = validate_bundle(bundle)
    paths = {entry.path for entry in manifest.entries}
    assert "state/context/handoff/switch-1/manifest.json" in paths
    assert "state/context/handoff/switch-1/handoff.md" in paths

    target_root = tmp_path / "target"
    target_root.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "target.sqlite3",
        owner="owner",
        machine="target-node",
        hyprial_home=target_root,
    )
    try:
        result = land_bundle_into_registry(
            bundle,
            registry=registry,
            home_root=target_root / "agents",
            staging=tmp_path / "landing-stage",
            granted_by="user:owner",
            expected_actor="worker",
            signature=UNSIGNED_REHEARSAL,
        )
        landed_handoff = result.destination / "state/context/handoff/switch-1"
        assert (landed_handoff / "manifest.json").read_text() == '{"schemaVersion":1}\n'
        assert registry.destroy("worker") is True
        assert not result.destination.exists()
    finally:
        registry.close()

    plan = plan_cleanup(source, actor="worker", owner="owner")
    receipt = execute_cleanup(
        plan,
        target_receipt="target-confirmed",
        bundle_dir=bundle,
    )
    assert receipt.complete is True
    assert receipt.uncovered == ()
    assert not source.exists()
