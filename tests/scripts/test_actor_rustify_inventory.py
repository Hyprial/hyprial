# Decision record (file header; no inline comments by repository rule):
# - P4 batch B2 (orgfs, 2026-11-10) moves 25 engine-holding sites to an
#   injected ActorHost: actor_runtime 61->52, effect_lane 47->37,
#   event_sink_runtime 10->4, site total 208->183. The measured delta lies
#   entirely inside the B2 orgfs files; no site was added or retargeted
#   outside them. This pin follows the deliberate migration like the P4
#   exemption ledger, and must move only with a batch that owns the change.
# - P4 batch B3 (lark/transport/transfer/network/worker control, 2026-11-10)
#   moves 20 engine-holding sites to an injected ActorHost: actor_runtime
#   52->40, effect_lane 37->30, event_sink_runtime 4->3, site total 183->163.
#   All 20 removed sites lie inside the twelve B3 files. The composition root
#   adds one generation_scheduler factory site (composition/actors/__init__.py)
#   while session_actor.py loses one, so that kind is net 0: an approved
#   composition-root exception, not part of the twelve-file delta.
# - P4 batch B6 (2026-11-11) removes 5 inventory sites across the four files
#   that safely migrated (application/__init__, operator_alarm,
#   service_connect/io, worker_state): effect_lane 30->26, generation_scheduler
#   7->6, site total 163->158. usage_actor, hooks/bus and log/writer were
#   reverted after independent verification showed the borrowed
#   AgentSessionDomains host outlived neither usage stop nor logger close; they
#   contribute no delta and keep their original sites. The four B6-blocked
#   files also retain their original sites. Other kinds are unchanged.
# - P4 batch B1 (2026-11-11): ten migrated files remove 17 sites:
#   actor_runtime 40->30, effect_lane 26->19, total 158->141. The two
#   process holders stay blocked on the B5 seam; all other kinds are unchanged.
# - P4 batch B6 restoration (2026-11-11), after the lifecycle close-order fix
#   (#2692): usage_actor, hooks/bus and log/writer migrate to the injected host
#   and remove 6 sites: actor_runtime 30->28, effect_lane 19->17,
#   generation_scheduler 6->5, event_sink_runtime 3->2, total 141->135. All
#   removed sites lie inside those three files; no other kind changes.
# - P4 batch B4 (lifecycle/autoupdate/watchdogs, 2026-11-11, #2698) moves six
#   sites to an injected ActorHost: actor_runtime 28->25, effect_lane 17->15,
#   generation_scheduler 5->4, site total 135->129. All six removed sites lie
#   inside the batch's source files (autoupdate/actor.py,
#   lifecycle/lifecycle_coordinator.py, operations/watchdog_actor.py); no other
#   kind changes and no site is added.
# - P4 batch B5 harness-actor host seam (2026-11-11) moves eight sites to an
#   injected ActorHost: actor_runtime 25->21, effect_lane 15->11, site total
#   129->121. All removed sites lie inside the six files that left the
#   exemption ledger (orphan_processes, process_owner, harnesses/actor/actor,
#   session_actor/actor, desired_state_io,
#   harnesses/actor/behaviors/delivery); no other kind changes.
# - P4 batch B4 remaining files (2026-11-11) moves 18 sites to an injected
#   ActorHost: actor_runtime 21->11, effect_lane 11->5,
#   generation_scheduler 4->2, site total 121->103. All 18 removed sites lie
#   inside the ten B4 files (user_delivery actor/ledger_actor,
#   configuration/home_lease_actor, forwarding authority/supervisor,
#   identity/profiles, lifecycle duplicate_actor/maintenance_watchdog,
#   operations/management_actor, state_persistence/authority); no site is
#   added or retargeted outside them.
# - P4 batch B5 remaining files (2026-11-12) moves 15 sites to an injected
#   ActorHost: actor_runtime 11->5, effect_lane 5->0, effect_lane_generic
#   2->0, generation_scheduler 2->1, event_sink_runtime 2->1, site total
#   103->88. All 15 removed sites lie inside the seven B5 files (inbox
#   actor/coordinator, inbox tracking/fetch_receipts, org/mesh,
#   org/network/casdoor/sync, pac/held_reminder/core,
#   identity/agents/actor/_core, identity/provider_auth/actor/_authority);
#   the three blocked B5 entries retain their sites and are deferred to P7.
#   The two generic EffectLane constructions pinned in AgentActor migrated to
#   actor_host.effect_lane: the subscripted-generic scanner contract is now
#   pinned on a synthetic source and the AgentActor pin asserts the host seam.
# - P7 single-host (2026-11-12) moves the remaining engine holders in five
#   files to the injected ActorHost: actor_runtime 5->0,
#   event_sink_runtime 1->0, site total 88->82. The removed sites are in
#   biz/routine execution coordinator+service, pac/graphs authority,
#   harnesses/carrier interactive_carrier_runtime and
#   harnesses/turn_delivery/turn_runtime; no other kind changes and no site
#   is added.
# - Slice 7 removed the transfer P2 lane (admission/journal actors and their
#   source effects): actor_spec 61->59, undelivered_sink 13->11, total 82->78.
# - Cell model I5-1 (2026-11-21) adds one keyed ActorSpec site, the cell actor
#   registry in daemon/impl/cells/actor/registry.py, spawned through the
#   injected ActorHost: actor_spec 59->60, total 78->79. No other kind changes.
# - Org rework OC2 (2026-11-23) adds one keyed ActorSpec site, the per-org
#   OrgCellActor in daemon/impl/org/cell_actor/actor.py, spawned through
#   the injected ActorHostRegistry.host_for("org-cell"): actor_spec 60->61,
#   total 79->80. No other kind changes.
# - Cell model I6-2 (2026-11-23) adds one keyed ActorSpec site, the per-cell
#   reconcile actor in daemon/impl/cells/reconcile/actor.py, spawned through
#   the injected ActorHostRegistry.host_for("cell-reconcile"): actor_spec
#   61->62, total 80->81. No other kind changes.
# - I7b-2 (2026-11-24) deletes the harness forward settlement actor
#   (runtime/settlement.py ForwardSettlementCoordinator) with the user-proxy
#   forward lane: actor_spec 62->61, total 81->80. No other kind changes.
# - S3c (2026-11-24) deletes the user profile authority actor
#   (daemon/impl/identity/profiles.py UserProfileAuthority) with the users
#   profile: actor_spec 61->60, total 80->79. No other kind changes.
# - OrgFS package S (2026-11-25) deletes the journal store's space authority
#   (orgfs/storage/space_authority.py), the replica authority
#   (orgfs/storage/replica_authority.py) and the journal mesh ingress actor
#   (orgfs/mesh.py): actor_spec 60->57, total 79->76. No other kind changes.
"""Pins the actor-rustify P1 inventory counts and determinism contract."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify" / "actor_rustify_inventory.py"
SPEC = importlib.util.spec_from_file_location("actor_rustify_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["actor_rustify_inventory"] = MODULE
SPEC.loader.exec_module(MODULE)

EXPECTED_COUNTS = {
    "actor_runtime": 0,
    "actor_spec": 57,
    "effect_lane": 0,
    "effect_lane_generic": 0,
    "effect_lane_variant": 1,
    "generation_scheduler": 1,
    "undelivered_sink": 11,
    "event_sink_runtime": 0,
    "expected_actor_error": 6,
}
EXPECTED_SITE_TOTAL = 76
EFFECT_SITE_KINDS = frozenset(
    {"effect_lane", "effect_lane_generic", "effect_lane_variant"}
)
WATCHED_USAGE = frozenset(
    {
        "submit",
        "reserve",
        "submit_reserved",
        "cancel_reservation",
        "acknowledge",
        "close",
        "snapshot",
        "schedule",
        "cancel",
        "shutdown",
        "drain",
    }
)


def _scan() -> dict[str, object]:
    return MODULE.scan(REPO_ROOT)


@pytest.fixture(scope="session")
def report() -> dict[str, object]:
    """One read-only scan for the ordinary assertions; determinism compares a fresh one."""
    return _scan()


def test_counts_match_p1_baseline(report: dict[str, object]) -> None:
    assert report["counts"] == EXPECTED_COUNTS
    assert report["engine_abi_version"] == 1
    assert len(report["sites"]) == EXPECTED_SITE_TOTAL


def test_scan_is_deterministic(report: dict[str, object]) -> None:
    assert json.dumps(report, sort_keys=False) == json.dumps(_scan(), sort_keys=False)


def test_every_site_has_owner_context_and_roles(report: dict[str, object]) -> None:
    for site in report["sites"]:
        assert site["path"].startswith("src/hyprial/")
        assert site["line"] >= 1
        assert site["roles"], f"unattributed site: {site}"
        assert set(site["roles"]) <= {"cli", "daemon", "worker", "mcp"}
        if site["kind"] == "expected_actor_error" and site["owner_function"] is None \
                and site["owner_class"] is None:
            continue
        assert site["owner_class"] is not None or site["owner_function"] is not None


def test_undelivered_sink_files_match_p1_scope(report: dict[str, object]) -> None:
    paths = sorted(
        {
            site["path"]
            for site in report["sites"]
            if site["kind"] == "undelivered_sink"
        }
    )
    assert len(paths) == 11
    assert all(path.startswith("src/hyprial/daemon/") or
               path.startswith("src/hyprial/identity/") for path in paths)


def test_subscripted_generic_lane_constructions_are_recorded() -> None:
    source = (
        "class Owner:\n"
        "    def __init__(self):\n"
        "        self._state_effects = EffectLane[_Request, _Result](\n"
        "            name='state', execute=None, complete=None\n"
        "        )\n"
    )
    collector = MODULE.SiteCollector("synthetic.py", ("daemon",))
    collector.visit(ast.parse(source))
    generic = [site for site in collector.sites if site.kind == "effect_lane_generic"]
    assert len(generic) == 1
    assert generic[0].receiver == "self._state_effects"
    assert generic[0].owner_class == "Owner"
    assert generic[0].owner_function == "__init__"


def test_agent_actor_lanes_come_from_the_injected_host(
    report: dict[str, object],
) -> None:
    path = "src/hyprial/identity/impl/agents/actor/_core.py"
    assert not [
        site for site in report["sites"]
        if site["path"] == path and site["kind"] == "effect_lane_generic"
    ]
    tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
    receivers = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.IfExp):
            value = value.body
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "effect_lane"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "actor_host"
        ):
            continue
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        receivers[ast.unparse(target)] = value.lineno
    assert set(receivers) == {"self._migration_effects"}


def test_effect_site_extraction_contract(report: dict[str, object]) -> None:
    for site in report["sites"]:
        if site["kind"] not in EFFECT_SITE_KINDS and \
                site["kind"] != "generation_scheduler":
            continue
        assert isinstance(site["kwargs"], dict)
        assert set(site["usage"]) <= WATCHED_USAGE
        if "submit_reserved" in site["usage"]:
            assert "reserve" in site["usage"], site


def test_cross_file_state_effects_is_t2() -> None:
    source = (
        REPO_ROOT
        / "src/hyprial/identity/impl/agents/actor/_generation_ops.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    aliases: set[str] = set()
    methods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "state_effects":
            aliases.add(node.targets[0].id)
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        value = node.func.value
        receiver: str | None = None
        if isinstance(value, ast.Attribute) and value.attr == "state_effects":
            receiver = "state_effects"
        elif isinstance(value, ast.Name) and value.id in aliases:
            receiver = "state_effects"
        if receiver is not None and node.func.attr in WATCHED_USAGE:
            methods.add(node.func.attr)
    assert methods == set()


def test_engine_internals_are_not_holders(report: dict[str, object]) -> None:
    for site in report["sites"]:
        assert "kernel/impl/actor_runtime" not in site["path"]
        assert "DaemonActorRuntime" not in json.dumps(site)


def test_cli_output_is_one_json_object_with_ok_shape(tmp_path: Path) -> None:
    output = tmp_path / "inventory.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(REPO_ROOT),
         "--output", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema"] == "actor-rustify-inventory-v2"
    assert written["counts"] == EXPECTED_COUNTS
