"""AT07: the dry run and the landing must answer the same question.

Two-machine reading 2026-10-12 (codex-sw): ``transfer-bundle-land --dry-run``
answered ``ok`` for a bundle that the real landing refuses -- the operator's
pre-flight said yes to a landing that could only fail.  ``materialise_payload``
refuses a payload outside ``HOME_TOPOLOGY`` while it writes; the dry run never
ran that check.  These arms hold the two answers together.

Kept in its own module because the sibling landing suite sits at the file's
structural line limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.landing.owner import LandingError, LandingFailed
from tests.integration.transfer.landing.test_transfer_registry_landing import (
    ACTOR,
    _bundle,
    _land,
    _registry,
    _source_home,
)


def test_dry_run_refuses_a_payload_outside_the_home_topology(tmp_path: Path) -> None:
    """The dry run used to answer ``ok`` for a bundle the landing refuses."""

    home = _source_home(tmp_path)
    (home / "note.txt").write_text("stray\n")
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingError) as caught:
            _land(tmp_path, bundle_dir, registry, dry_run=True)

        assert "note.txt" in str(caught.value)
    finally:
        registry.close()


def test_dry_run_and_landing_refuse_the_same_stray_payload(tmp_path: Path) -> None:
    """What the dry run passes is what the landing takes -- and vice versa."""

    home = _source_home(tmp_path)
    (home / "note.txt").write_text("stray\n")
    bundle_dir = _bundle(tmp_path, home)
    registry = _registry(tmp_path)
    try:
        with pytest.raises(LandingError) as preview:
            _land(tmp_path, bundle_dir, registry, dry_run=True)
        with pytest.raises(LandingFailed) as landed:
            _land(tmp_path, bundle_dir, registry)

        assert "note.txt" in str(preview.value)
        assert "note.txt" in str(landed.value)
        assert registry.exists(ACTOR) is False
        assert not (tmp_path / "agents" / ACTOR).exists()
        assert not (tmp_path / "staging" / "landing-1").exists()
    finally:
        registry.close()
