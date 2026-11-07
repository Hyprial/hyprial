"""Structural and ownership proofs for the harness-actor deep-module split."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from hyprial.daemon.impl.harnesses import actor as actor_package
from hyprial.daemon.impl.harnesses.actor import (
    HarnessProjection,
    HarnessRuntimeActor,
    HarnessRuntimeClosed,
    HarnessRuntimeFacade,
    ProcessIdentity,
    ProcessIoPort,
)
from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor as OwnerActor
from hyprial.daemon.impl.harnesses.actor.behaviors.delivery import (
    _DeliveryBehavior,
)
from hyprial.daemon.impl.harnesses.actor.behaviors.desired import _DesiredBehavior
from hyprial.daemon.impl.harnesses.actor.behaviors.lifecycle import (
    _LifecycleBehavior,
)
from hyprial.daemon.impl.harnesses.actor.behaviors.process_events import (
    _ProcessEventsBehavior,
)
from hyprial.daemon.impl.harnesses.actor.behaviors.reconcile import (
    _ReconcileBehavior,
)


SOURCE_ROOT = Path(__file__).resolve().parents[4] / 'src/hyprial'
ACTOR_ROOT = SOURCE_ROOT / "daemon" / "impl" / "harnesses" / "actor"
BEHAVIORS_ROOT = ACTOR_ROOT / "behaviors"
LEGACY_FLAT = SOURCE_ROOT / "daemon" / "impl" / "harness_actor.py"

MIXINS = (
    _DesiredBehavior,
    _ProcessEventsBehavior,
    _DeliveryBehavior,
    _ReconcileBehavior,
    _LifecycleBehavior,
)


def _direct_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file())


def test_legacy_flat_module_is_removed() -> None:
    assert not LEGACY_FLAT.exists()
    assert not (SOURCE_ROOT / "daemon" / "harness_actor.py").exists()


def test_package_is_the_actual_module_owner() -> None:
    assert HarnessRuntimeActor.__module__ == "hyprial.daemon.impl.harnesses.actor.actor"
    assert HarnessRuntimeFacade.__module__ == "hyprial.daemon.impl.harnesses.actor.facade"
    assert ProcessIoPort.__module__ == "hyprial.daemon.impl.harnesses.actor.io_port"
    assert HarnessProjection.__module__ == "hyprial.daemon.impl.harnesses.actor.contracts"
    assert ProcessIdentity.__module__ == "hyprial.daemon.impl.harnesses.actor.contracts"
    assert HarnessRuntimeClosed.__module__ == "hyprial.daemon.impl.harnesses.actor.contracts"
    assert OwnerActor is HarnessRuntimeActor


def test_importing_package_does_not_recreate_flat_forwarder() -> None:
    importlib.import_module("hyprial.daemon.impl.harnesses.actor")
    assert "hyprial.daemon.impl.harness_actor" not in sys.modules


def test_owned_directories_respect_structural_limits() -> None:
    for directory in (ACTOR_ROOT, BEHAVIORS_ROOT):
        files = _direct_files(directory)
        assert len(files) <= 7, (directory, [f.name for f in files])
        for path in files:
            assert path.suffix == ".py"
            lines = path.read_text(encoding="utf-8").splitlines()
            assert len(lines) <= 1000, (path, len(lines))


def test_canonical_actor_composes_every_behaviour_mixin() -> None:
    for mixin in MIXINS:
        assert mixin in HarnessRuntimeActor.__mro__
    # base owner wins; mixins never define __init__ or a second state host.
    assert HarnessRuntimeActor.__init__.__module__ == (
        "hyprial.daemon.impl.harnesses.actor.actor"
    )
    for mixin in MIXINS:
        assert "__init__" not in mixin.__dict__


def test_method_ownership_is_semantic_and_disjoint() -> None:
    expected = {
        "receive": "actor",
        "_publish": "actor",
        "_continue_lifecycle_apply": "behaviors.lifecycle",
        "_on_reconcile": "behaviors.reconcile",
        "_on_stop_all": "behaviors.delivery",
        "_on_desired_completed": "behaviors.desired",
        "_begin_start": "behaviors.process_events",
        "_on_fail_lifecycle": "behaviors.process_events",
        "_on_manage_desired": "behaviors.reconcile",
        "close_runtime": "behaviors.lifecycle",
        "_on_call_completed": "behaviors.delivery",
        "_emit_event": "behaviors.desired",
    }
    for method, owner in expected.items():
        assert getattr(HarnessRuntimeActor, method).__module__ == (
            f"hyprial.daemon.impl.harnesses.actor.{owner}"
        ), (method, owner)

    seen: dict[str, str] = {}
    for mixin in MIXINS:
        module = mixin.__module__
        for name, value in mixin.__dict__.items():
            if not callable(value) or name.startswith("__"):
                continue
            assert name not in seen, (name, seen.get(name), module)
            seen[name] = module
    assert "receive" not in seen
    assert "_on_call_completed" in seen


def test_package_exports_are_explicit() -> None:
    assert set(actor_package.__all__) >= {
        "HarnessProjection",
        "HarnessRestoreSummary",
        "HarnessRuntimeActor",
        "HarnessRuntimeClosed",
        "HarnessRuntimeFacade",
        "ProcessIdentity",
        "ProcessIoPort",
        "process_identities_match",
    }
    for name in actor_package.__all__:
        assert hasattr(actor_package, name)
