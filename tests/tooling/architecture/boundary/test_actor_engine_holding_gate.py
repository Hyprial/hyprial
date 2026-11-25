# Decisions (actor-rustify P4, P8):
# - The repository checks run the gate against the real tree.  P7 emptied the
#   holder list and P8 deleted the compatibility facade and the shrinking-only
#   manifest/ceiling machinery, so the repository assertion is simply that no
#   file outside the engine region and the composition roots holds the engine.
# - The recall list below is written by hand from the 2026-11-04 holder survey
#   and deliberately does not reuse the scanner, so a scanner that goes blind
#   (finds nothing) or over-matches (flags wrappers and re-exports) is red.  A
#   regex cross-check, independent of the AST walk, also requires every file
#   with a literal held-name call to be reported as a holder.
# - RED evidence is mechanical: the real-tree mutation copies src/hyprial and
#   adds a construction to a known non-holder, which must turn the gate red.
# - P7 (2026-11-13): the last five holders leave (5/5 -> 0/0) after the
#   routine service/coordinator, PAC graph authority and both CLI carrier
#   runtimes take cells from an injected/owned ActorHostCustody.  The only
#   literal held-name call left is the composition root's GenerationScheduler.
# - P8 (2026-11-15): ActorRuntime, the compatibility facade, is deleted, so the
#   held-name set no longer carries it; mutation and recall fixtures use the
#   retained engine names.
"""Engine-holding rule: an empty holder list and mutation proofs."""

from __future__ import annotations

import ast
import re
import shutil
from pathlib import Path

from tests.support.gates.actor_boundary_gate import (
    ENGINE_COMPOSITION_ROOTS,
    ENGINE_HELD_NAMES,
    ENGINE_REEXPORT_SURFACES,
    engine_holders,
    scan_actor_boundaries,
    validate_engine_allowlists,
)

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

KNOWN_HOLDERS: dict[str, frozenset[str]] = {}

KNOWN_NON_HOLDERS = (
    "daemon/impl/dispatch/runtime/holds.py",
    "daemon/impl/application/__init__.py",
    "daemon/impl/application/messaging/operator_alarm.py",
    "daemon/impl/service_connect/io.py",
    "identity/impl/agents/state/worker_state.py",
    "biz/impl/usage_actor.py",
    "kernel/impl/log/writer.py",
    "kernel/impl/hooks/bus.py",
    "daemon/impl/transport/session_actor.py",
    "kernel/__init__.py",
    "kernel/interface.py",
    "daemon/impl/pac/actors/coordinator/__init__.py",
    "daemon/impl/pac/actors/coordinator/core.py",
    "daemon/impl/pac/actors/coordinator/types.py",
    "daemon/impl/pac/actors/daemon.py",
    "daemon/impl/application/wiring/services.py",
    "daemon/impl/harnesses/streaming/failure.py",
    "daemon/impl/harnesses/codex/carrier.py",
    "identity/impl/agents/home/effects.py",
    "daemon/impl/processes/orphan_processes.py",
    "daemon/impl/processes/process_owner.py",
    "daemon/impl/harnesses/actor/actor.py",
    "daemon/impl/session_actor/actor.py",
    "daemon/impl/desired_state_io.py",
    "daemon/impl/harnesses/actor/behaviors/delivery/__init__.py",
    "daemon/impl/inbox/actor/coordinator.py",
    "daemon/impl/inbox/tracking/fetch_receipts.py",
    "daemon/impl/org/mesh.py",
    "daemon/impl/org/network/casdoor/sync.py",
    "daemon/impl/pac/held_reminder/core.py",
    "identity/impl/agents/actor/_core.py",
    "identity/impl/provider_auth/actor/_authority.py",
    "biz/impl/routine/execution/coordinator/__init__.py",
    "biz/impl/routine/execution/service.py",
    "daemon/impl/harnesses/carrier/interactive_carrier_runtime.py",
    "daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py",
    "daemon/impl/pac/graphs/authority/__init__.py",
)

_LITERAL_CALL = re.compile(r"\b(ActorGuardian|EffectLane|GenerationScheduler)(\[[^\]\n]*\])?\(")


def _label(relative: str) -> str:
    return f"src/hyprial/{relative}"


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _held(root: Path, **kwargs: frozenset[Path]) -> dict[str, frozenset[str]]:
    return engine_holders(root, **kwargs)


def test_repository_engine_holder_list_is_empty() -> None:
    assert engine_holders(SRC) == {}


def test_repository_engine_allowlists_name_real_files() -> None:
    assert validate_engine_allowlists(SRC) == []
    assert ENGINE_COMPOSITION_ROOTS == {
        Path("daemon/impl/composition/actors/__init__.py")
    }
    assert ENGINE_REEXPORT_SURFACES == {Path("kernel/__init__.py"), Path("kernel/interface.py")}


def test_composition_roots_construct_a_host_and_are_not_exempt() -> None:
    holders = engine_holders(SRC)
    for relative in sorted(ENGINE_COMPOSITION_ROOTS):
        path = SRC / relative
        assert path.is_file(), relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        built = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "create_actor_host"
        ]
        assert built, f"{relative} is allowlisted but does not call create_actor_host"
        assert _label(str(relative)) not in holders, relative


def test_held_names_include_the_port_objects_by_name() -> None:
    assert {"create_engine", "create_actor_host", "ActorHost"} <= ENGINE_HELD_NAMES
    assert {"ActorGuardian", "GenerationScheduler", "EffectLane"} <= ENGINE_HELD_NAMES
    assert "ActorRuntime" not in ENGINE_HELD_NAMES


def test_hand_written_recall_list_matches_the_scanner() -> None:
    holders = engine_holders(SRC)
    for relative, names in KNOWN_HOLDERS.items():
        assert (SRC / relative).is_file(), relative
        assert holders.get(_label(relative)) == names, relative
    for relative in KNOWN_NON_HOLDERS:
        assert (SRC / relative).is_file(), relative
        assert _label(relative) not in holders, relative


def test_every_literal_engine_call_is_reported_by_the_ast_scan() -> None:
    holders = engine_holders(SRC)
    literal: set[str] = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC)
        if relative.is_relative_to("kernel/impl/actor_runtime"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("#", "class ", "def ")):
                continue
            if _LITERAL_CALL.search(line):
                literal.add(_label(str(relative)))
    allowed_roots = {_label(str(relative)) for relative in ENGINE_COMPOSITION_ROOTS}
    # P7 emptied the manifest.  The only literal held-name call left is the
    # composition root's GenerationScheduler; any other literal call is a
    # holder the AST scan must have reported.
    assert len(literal) >= len(allowed_roots)
    assert literal <= set(holders) | allowed_roots, sorted(literal - set(holders) - allowed_roots)


def test_real_tree_mutation_turns_the_gate_red(tmp_path: Path) -> None:
    tree = tmp_path / "hyprial"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__", "_gui", "*.pyc"))
    carrier = tree / "daemon/impl/harnesses/codex/carrier.py"
    carrier.write_text(
        carrier.read_text(encoding="utf-8")
        + "\n\ndef _mutant() -> object:\n"
        "    from hyprial.kernel import EffectLane\n\n"
        "    return EffectLane(capacity=1)\n",
        encoding="utf-8",
    )
    holds = tree / "daemon/impl/pac/graphs/authority/__init__.py"
    holds.write_text(
        holds.read_text(encoding="utf-8")
        + "\n\ndef _mutant_scheduler() -> object:\n"
        "    from hyprial.kernel import GenerationScheduler\n\n"
        "    return GenerationScheduler()\n",
        encoding="utf-8",
    )
    violations = scan_actor_boundaries(tree)
    assert {(item.rule, item.path) for item in violations} == {
        ("engine-holding", "src/hyprial/daemon/impl/harnesses/codex/carrier.py"),
        ("engine-holding", "src/hyprial/daemon/impl/pac/graphs/authority/__init__.py"),
    }


def test_mutation_construction_in_a_non_allowlisted_file_is_red(tmp_path: Path) -> None:
    _write(tmp_path, "daemon/impl/owner.py", "from hyprial.kernel import EffectLane\nlane = EffectLane[int](capacity=1)\n")
    violations = scan_actor_boundaries(tmp_path)
    assert {item.detail for item in violations} == {"import EffectLane", "construct EffectLane"}
    assert {item.rule for item in violations} == {"engine-holding"}


def test_mutation_every_held_name_and_spelling_is_a_holding(tmp_path: Path) -> None:
    _write(tmp_path, "daemon/impl/a.py", "from hyprial.kernel.impl.actor_runtime.port import create_engine, create_actor_host, ActorHost\n")
    _write(tmp_path, "daemon/impl/b.py", "from hyprial.kernel import ActorGuardian as Guardian\n")
    _write(tmp_path, "daemon/impl/c.py", "import hyprial.kernel as k\nk.GenerationScheduler()\n")
    _write(tmp_path, "daemon/impl/d.py", "import hyprial.kernel as k\nfactory = getattr(k, 'ActorGuardian')\n")
    _write(tmp_path, "kernel/impl/e.py", "from ..impl.actor_runtime import EffectLane\n")
    _write(tmp_path, "daemon/impl/f.py", "from hyprial.kernel.impl import actor_runtime\n")
    _write(tmp_path, "daemon/impl/g.py", "def build(port):\n    return port.create_actor_host()\n")
    assert _held(tmp_path) == {
        _label("daemon/impl/a.py"): {"create_engine", "create_actor_host", "ActorHost"},
        _label("daemon/impl/b.py"): {"ActorGuardian"},
        _label("daemon/impl/c.py"): {"GenerationScheduler"},
        _label("daemon/impl/d.py"): {"ActorGuardian"},
        _label("kernel/impl/e.py"): {"EffectLane"},
        _label("daemon/impl/f.py"): {"hyprial.kernel.impl.actor_runtime"},
        _label("daemon/impl/g.py"): {"create_actor_host"},
    }


def test_mutation_subclasses_and_shadows_are_holdings(tmp_path: Path) -> None:
    _write(tmp_path, "kernel/impl/actor_runtime/effects.py", "class EffectLane:\n    pass\n\nclass TimedLane(EffectLane):\n    pass\n")
    _write(tmp_path, "daemon/impl/user.py", "from hyprial.kernel import TimedLane\nTimedLane()\n")
    _write(tmp_path, "daemon/impl/sub.py", "from hyprial.kernel import EffectLane\n\nclass MyLane(EffectLane[int]):\n    pass\n")
    _write(tmp_path, "daemon/impl/sub_user.py", "from hyprial.daemon.impl.sub import MyLane\nMyLane()\n")
    _write(tmp_path, "daemon/impl/pac.py", "from typing import Protocol\n\nclass GenerationScheduler(Protocol):\n    pass\n")
    assert _held(tmp_path) == {
        _label("daemon/impl/user.py"): {"TimedLane"},
        _label("daemon/impl/sub.py"): {"EffectLane", "MyLane"},
        _label("daemon/impl/sub_user.py"): {"MyLane"},
        _label("daemon/impl/pac.py"): {"GenerationScheduler"},
    }


def test_mutation_non_holdings_stay_green(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "daemon/impl/typed.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from hyprial.kernel import EffectLane\n",
    )
    _write(tmp_path, "daemon/impl/foreign.py", "from sqlalchemy import create_engine\ncreate_engine('sqlite://')\n")
    _write(tmp_path, "daemon/impl/contracts.py", "from hyprial.kernel.impl.actor_runtime import AdmissionResult\n")
    _write(tmp_path, "daemon/impl/wrapper.py", "class TurnRuntime:\n    pass\n\nDaemonScheduler = TurnRuntime\n")
    _write(tmp_path, "kernel/__init__.py", "from hyprial.kernel.impl.actor_runtime import EffectLane as EffectLane\n")
    _write(tmp_path, "daemon/impl/root.py", "from hyprial.kernel import EffectLane\nEffectLane()\n")
    assert _held(tmp_path, composition_roots=frozenset({Path("daemon/impl/root.py")})) == {}


def test_mutation_reexport_surface_may_import_but_not_construct(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "kernel/interface.py",
        "from hyprial.kernel.impl.actor_runtime import EffectLane\nDEFAULT = EffectLane(capacity=1)\n",
    )
    holdings = scan_actor_boundaries(tmp_path)
    assert [(item.path, item.detail) for item in holdings] == [
        (_label("kernel/interface.py"), "construct EffectLane")
    ]


def test_mutation_stale_allowlist_is_red(tmp_path: Path) -> None:
    errors = validate_engine_allowlists(
        tmp_path,
        composition_roots=frozenset({Path("daemon/impl/composition/domains.py")}),
        reexport_surfaces=frozenset({Path("kernel/impl/actor_runtime/port/__init__.py")}),
    )
    assert errors == [
        "stale engine composition root allowlist entry: src/hyprial/daemon/impl/composition/domains.py",
        "engine re-export surface kernel/impl/actor_runtime/port/__init__.py is inside the engine region already",
    ]
