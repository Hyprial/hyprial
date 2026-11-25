#!/usr/bin/env python3
"""E2E-003: adapter cell-binding lifecycle through real CLI/IPC across restarts.

The binding is the agent's cell document (an adapter member whose
``cell.members.<ref>.adapter`` names the local adapter configuration); the
daemon projects it into ``adapter.list`` through the cell routing index.
This scenario proves, against a real daemon:

1. binding requires an existing agent (AGENT_NOT_FOUND before ``agent create``);
2. a cell apply binds the adapter, is visible in ``adapter list`` at once, and
   survives a daemon restart;
3. the same adapter declared by two agent cells is a routing conflict and
   fails closed (``agent: null``), not a write-time refusal;
4. ``agent destroy`` removes the binding with the agent's cell;
5. removing the member with a second cell apply survives a restart.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable

NODE = "pin-node"
AGENT = "pin-worker"


def _add_adapter(daemon: IsolatedDaemon, root: Path, name: str) -> None:
    secret_file = root / f"secret-{name}.txt"
    secret_file.write_text("e2e-secret\n", encoding="utf-8")
    added = daemon.command(
        "adapter",
        "add",
        name,
        "--app-id",
        "cli_e2e_pin",
        "--route",
        "dm=oc_e2e_pin_dm",
        "--secret-file",
        str(secret_file),
        "--json",
    )
    if added.get("ok") is not True:
        raise AssertionError(f"adapter add failed: {added}")


def _bound_agents(daemon: IsolatedDaemon) -> dict[str, str | None]:
    listed = daemon.command("adapter", "list", "--json")
    return {
        str(item["name"]): item.get("agent")
        for item in listed.get("adapters", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _cell(daemon: IsolatedDaemon, name: str) -> dict:
    shown = daemon.command("agent", "cell", "show", name, "--json")
    cell = shown.get("cell") if isinstance(shown, dict) else None
    if not isinstance(cell, dict) or not isinstance(cell.get("revision"), int):
        raise AssertionError(f"cell show did not answer a document: {shown}")
    return cell


def _cell_apply(
    daemon: IsolatedDaemon, root: Path, name: str, cell: dict, fragment: dict
) -> dict:
    fragment_path = root / f"cell-{name}.json"
    fragment_path.write_text(json.dumps(fragment), encoding="utf-8")
    applied = daemon.command(
        "agent",
        "cell",
        "apply",
        name,
        "--expected-revision",
        str(cell["revision"]),
        "--file",
        str(fragment_path),
        "--json",
    )
    if applied.get("ok") is not True:
        raise AssertionError(f"cell apply failed: {applied}")
    return applied


def _bind(daemon: IsolatedDaemon, root: Path, name: str, adapter: str) -> dict:
    cell = _cell(daemon, name)
    members = list(cell.get("members") or [])
    params = dict(cell.get("params") or {})
    member_data = dict(params.get("cell.members") or {})
    member_data[adapter] = {"adapter": adapter}
    params["cell.members"] = member_data
    if "cell.entry" not in params and members:
        params["cell.entry"] = members[0]["ref"]
    return _cell_apply(
        daemon,
        root,
        name,
        cell,
        {
            "members": [*members, {"ref": adapter, "role": "adapter"}],
            "params": params,
        },
    )


def _unbind(daemon: IsolatedDaemon, root: Path, name: str, adapter: str) -> dict:
    cell = _cell(daemon, name)
    members = [
        item for item in (cell.get("members") or []) if item.get("ref") != adapter
    ]
    params = dict(cell.get("params") or {})
    member_data = dict(params.get("cell.members") or {})
    member_data.pop(adapter, None)
    params["cell.members"] = member_data
    return _cell_apply(daemon, root, name, cell, {"members": members, "params": params})


def main() -> int:
    executable = hyprial_executable()
    with tempfile.TemporaryDirectory(prefix="hyprial-e2e-003-") as temporary:
        root = Path(temporary)
        daemon = IsolatedDaemon(executable, root, NODE)
        daemon.env["HYPRIAL_LARK_WORKER_COMMAND"] = json.dumps(
            [
                sys.executable,
                str(
                    Path(__file__).resolve().parents[2]
                    / "tests"
                    / "fixtures"
                    / "lark"
                    / "fake_lark_worker.py"
                ),
            ]
        )
        adapter = "pin-e2e"
        second_adapter = "pin-e2e-b"
        try:
            # #266: only `hyprial init` creates the home.  Until it exists, every
            # other command against this HYPRIAL_HOME must refuse -- lock that
            # guard in as a scenario step so the pre-#266 implicit-creation
            # behaviour can never silently come back.
            refused_home = daemon.command("adapter", "list", "--json", exits=(1,))
            if refused_home.get("code") != "HYPRIAL_HOME_NOT_INITIALIZED":
                raise AssertionError(
                    f"uninitialized home was not refused: {refused_home}"
                )
            # Adapters are a boot-time snapshot: configure before daemon start.
            # That ordering rules out `hyprial init` here (it also boots the
            # daemon), so pre-create the home directory the way the other
            # pre-start scenarios do since #266.
            (root / "home").mkdir(mode=0o700)
            _add_adapter(daemon, root, adapter)
            _add_adapter(daemon, root, second_adapter)
            daemon.start()

            owner = daemon.ps()["daemon"].get("owner")

            cell_file = root / "cell-ghost.json"
            cell_file.write_text(
                json.dumps(
                    {
                        "members": [{"ref": adapter, "role": "adapter"}],
                        "params": {"cell.members": {adapter: {"adapter": adapter}}},
                    }
                ),
                encoding="utf-8",
            )
            refused = daemon.command(
                "agent", "cell", "apply", "ghost",
                "--expected-revision", "1", "--file", str(cell_file),
                "--json", exits=(1,),
            )
            if refused.get("code") != "AGENT_NOT_FOUND":
                raise AssertionError(f"missing agent was not refused: {refused}")

            created = daemon.command(
                "agent", "create", "--name", AGENT, "--json"
            )
            if created.get("ok") is not True:
                raise AssertionError(f"agent create failed: {created}")
            listed = daemon.command("agent", "list", "--json")
            record = next(item for item in listed["agents"] if item["actor"] == AGENT)
            canonical = record["uri"]
            if not canonical.startswith(f"agent:{owner}:{NODE}:a."):
                raise AssertionError(f"agent did not publish an id address: {record}")

            applied = _bind(daemon, root, AGENT, adapter)
            if applied.get("revision") != 2:
                raise AssertionError(f"first apply did not advance the revision: {applied}")
            bound = _bound_agents(daemon)
            if bound.get(adapter) != canonical or bound.get(second_adapter) is not None:
                raise AssertionError(f"binding not visible in adapter list: {bound}")
            reapplied = _bind(daemon, root, AGENT, adapter)
            if reapplied.get("revision") != 3:
                raise AssertionError(f"re-apply did not advance the revision: {reapplied}")
            if _bound_agents(daemon).get(adapter) != canonical:
                raise AssertionError("re-apply changed the binding")

            second_name = f"{AGENT}-other"
            daemon.command("agent", "create", "--name", second_name, "--json")
            _bind(daemon, root, second_name, adapter)
            conflicted = _bound_agents(daemon)
            if conflicted.get(adapter) is not None:
                raise AssertionError(f"cross-cell conflict did not fail closed: {conflicted}")
            _unbind(daemon, root, second_name, adapter)
            if _bound_agents(daemon).get(adapter) != canonical:
                raise AssertionError("routing did not recover after unbinding the conflict")

            daemon.stop()
            daemon.start()
            recovered = _bound_agents(daemon)
            if recovered.get(adapter) != canonical:
                raise AssertionError(f"binding did not survive restart: {recovered}")

            destroyed = daemon.command(
                "agent", "destroy", AGENT, "--yes", "--json"
            )
            if destroyed.get("unboundAdapters") != [adapter]:
                raise AssertionError(f"destroy did not report the binding: {destroyed}")
            after_destroy = _bound_agents(daemon)
            if after_destroy.get(adapter) is not None:
                raise AssertionError(
                    f"binding outlived the destroyed agent: {after_destroy}"
                )

            fresh_name = AGENT
            recreated = daemon.command("agent", "create", "--name", fresh_name, "--json")
            if recreated.get("created") is not True:
                raise AssertionError(f"fresh agent create did not provision: {recreated}")
            listed = daemon.command("agent", "list", "--json")
            fresh = next(item for item in listed["agents"] if item["actor"] == fresh_name)
            fresh_uri = fresh["uri"]
            if fresh_uri == canonical or not fresh_uri.startswith(f"agent:{owner}:{NODE}:a."):
                raise AssertionError(f"fresh agent did not have a new id: {fresh}")
            _bind(daemon, root, fresh_name, adapter)
            if _bound_agents(daemon).get(adapter) != fresh_uri:
                raise AssertionError("recreated agent did not bind")
            _unbind(daemon, root, fresh_name, adapter)
            if _bound_agents(daemon).get(adapter) is not None:
                raise AssertionError("unbind did not detach the adapter")
            daemon.stop()
            daemon.start()
            final = _bound_agents(daemon)
            if final.get(adapter) is not None:
                raise AssertionError(f"unbind did not survive restart: {final}")
        finally:
            daemon.stop()
    print(
        "RESULT PASS E2E-003 adapter cell binding restart survival "
        "persisted=true conflict-fail-closed=true destroy-clears=true "
        "name-reused=true fresh-id=true unbound=true"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError) as error:
        print(f"RESULT FAIL E2E-003 {error}")
        raise SystemExit(1) from error
