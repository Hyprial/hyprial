#!/usr/bin/env python3
# Decision record (owner ruling, Allen 2026-11-07; UUID addressing P4, U1):
# - The P4 migration is one-way and has no compatibility window (U1): the
#   first open by this build rewrites every stored name-form agent URI to its
#   id form (docs/design/uuid-addressing-implementation-2026-11-03.md §2.4).
# - After P4 the ONLY supported downgrade is backup rollback: restore the
#   pre-upgrade backup of every store P4 rewrites, then run the old build.
# - Clean-row rollback (keep the migrated database, run the old build on it)
#   is no longer supported and no longer exercised here: the old build would
#   run with id-form addresses it cannot route.  The interim expectation that
#   the old build serves the id-form rows unchanged (option (a)) is withdrawn.
# - The product takes no automatic backup before P4 runs; this scenario
#   makes its own (RollbackScenario.save) the way the operator runbook
#   (docs/notes/runbook-agent-transfer-mvp-2026-09-25.md) tells operators to.
# - The old build started on the migrated rows is kept only as a negative
#   witness: it must refuse to start (fail closed), never serve them.
# - The invited real-worker destroy journey this scenario once ran was
#   deleted with the host-invite feature (owner ruling 2026-11-21); the
#   custody witness now plants a legacy foreign-owner hosted row by SQL, the
#   only shape that can still collide with an owner change.
# - P4_STORES covers the other rewritten state databases (adapter reply routes,
#   routine actors, PAC owners, and daemon stored_agent_uris); a store absent
#   at backup time must remain absent after restore.
# - After witnessing the failed direct downgrade, resume the new build from its
#   own last state before restoring the backup; the synthetic legacy row remains
#   only as setup and is removed by that backup restoration.
"""AT04: the one supported downgrade after P4 (backup rollback).

Use --old-tree pointing to a clean bd6eb960 checkout with its own .venv.
All generated credentials and business data are synthetic. Never upgrades the
installed package. The production socket is pinged only, by the shared runner.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

spec = importlib.util.spec_from_file_location(
    "hosted_registry", Path(__file__).with_name("run-hosted-registry.py")
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
hosted_spec = importlib.util.spec_from_file_location(
    "hosted_start", Path(__file__).with_name("run-hosted-start.py")
)
hosted = importlib.util.module_from_spec(hosted_spec)
hosted_spec.loader.exec_module(hosted)
OLD_SHA = "bd6eb9601ec3ee54b788d71ac7dfa3e0864fea72"
DATABASES = ("agents.sqlite3", "lifecycle-operations.sqlite3")
#: A store absent at backup time must be absent again after the restore.
P4_STORES = ("adapters.sqlite3", "routines.sqlite3", "pac-graph.sqlite3")
RESTORED = (*DATABASES, *P4_STORES)
#: agent:<owner>:<node>:a.<id> -- an address only this build mints or migrates to.
ID_FORM_URI = re.compile(r"agent:[^:\s\"']+:[^:\s\"']+:a\.[a-z2-7]+")

# Executed by the OLD interpreter, importing the OLD registry. This is setup,
# not an assertion that a cross-machine transfer happened.
SEED = """
import hashlib, json, os
from pathlib import Path
from hyprial.agents.registry import AgentRegistry
from hyprial.agents.secrets import SecretSource
import hyprial.agents.registry as module
source = Path(module.__file__).resolve()
assert source.is_relative_to(Path(os.environ['AT01_SOURCE']) / 'src')
home, state = Path(os.environ['HYPRIAL_HOME']), Path(os.environ['HARNESS_STATE_DIR'])
registry = AgentRegistry(state/'agents.sqlite3', owner='host-owner', machine='host-node', hyprial_home=home)
registry.create('native')
registry.create_transfer_hosted('hosted', pinned_owner='host-owner')
for name in ('native', 'hosted'):
    root = Path(registry.home_receipt(name).path)
    registry.ensure_workspace(name).joinpath('keep.txt').write_text('synthetic workspace '+name)
    (root/'secrets/fixture.json').write_text(json.dumps({'key': 'synthetic-only'}))
    registry.pin('synthetic-'+name, name)
    registry.grant_secret(name, grant_id='fixture', source=SecretSource.AGENT_PRIVATE,
        entry_id='fixture.json', field_name='key', environment_names=('OPENAI_API_KEY',), revision=1)
registry.close()
print(json.dumps({'module': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}))
"""


def id_form_uris(root):
    """Every id-form agent URI held by the state P4 rewrites under ``root``.

    The restored databases plus the HYPRIAL_HOME org state (org ACL grants);
    the message stores are not P4's (ruling X) and are deliberately not read.
    """
    found = set()
    for name in RESTORED:
        path = root / "state" / name
        if path.exists():
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                for line in db.iterdump():
                    found.update(ID_FORM_URI.findall(line))
            finally:
                db.close()
    org = root / "home/state/org"
    for path in sorted(org.rglob("*")) if org.exists() else ():
        if path.is_file():
            found.update(ID_FORM_URI.findall(path.read_text(errors="replace")))
    return found


def plant_legacy_foreign_hosted_row(root):
    """Insert by SQL the one row shape an owner change must still refuse.

    A foreign-owner hosted agent cannot be created through any public path
    since the host-invite deletion (teleport landing refuses cross-owner), so
    the negative witness plants the legacy residue directly: this is setup
    for the old build's custody gate, exactly like the old invite CLI left
    behind.
    """
    token = uuid.uuid4().hex
    uri = f"agent:visitor@example.test:host-node:{base.agent_id_for(token)}"
    with sqlite3.connect(root / "state/agents.sqlite3") as db:
        db.execute(
            "INSERT INTO agents(actor, owner, machine, uri, entity_token, cwd, "
            "capabilities, harness_args, created_at_ms, hosted_by, agent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, '{}', '{}', ?, 'host-invite', ?)",
            ("legacy-guest", "visitor@example.test", "host-node", uri, token,
             "/synthetic/legacy", 1700000000999, base.agent_id_for(token)),
        )
    return uri


class DestroyScenario(hosted.HostedPiScenario):
    scope = "local-owner real Pi worker destroy and incarnation cleanup; L0 only"

    def after_rounds(self):
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            old_token, uri = db.execute(
                "SELECT entity_token, uri FROM agents WHERE actor='worker'"
            ).fetchone()
        home = self.root / "home/agents/worker"
        (home / "workspace").mkdir(parents=True, exist_ok=True)
        (home / "workspace/sentinel.txt").write_text("synthetic-only")
        (home / "secrets").mkdir(mode=0o700, exist_ok=True)
        (home / "secrets/fixture.json").write_text('{"key":"synthetic-only"}')
        self.cli(
            "agent", "secret", "grant", "worker", "--grant-id", "fixture",
            "--source", "agent-private", "--entry-id", "fixture.json",
            "--field-name", "key", "--environment-name", "OPENAI_API_KEY",
            "--revision", "1",
        )
        running = hosted.request(self.socket, "ps", {})
        assert any(
            row.get("uri") == uri and row.get("status") != "offline"
            for row in running.get("agents", [])
        ), running
        with sqlite3.connect(self.root / "state/lifecycle-operations.sqlite3") as db:
            assert db.execute(
                "SELECT 1 FROM harnesses WHERE name='worker'"
            ).fetchone(), "running worker has no durable harness row"
        result = self.cli("agent", "destroy", "worker", "--yes")
        assert result["destroyed"] is True, result
        assert "pi:worker" in result.get("stopped", []), result
        assert not home.exists(), "destroy left the local agent home behind"
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            assert not db.execute(
                "SELECT 1 FROM agents WHERE actor='worker'"
            ).fetchone()
            assert not db.execute(
                "SELECT 1 FROM agent_capability_grants WHERE actor='worker'"
            ).fetchone()
            assert not db.execute(
                "SELECT 1 FROM agent_secret_grants WHERE agent='worker'"
            ).fetchone()
            resources = db.execute(
                "SELECT resource_key, active, payload FROM lifecycle_resources "
                "WHERE resource_key IN ('agent-record:worker', 'agent-home:worker')"
            ).fetchall()
            assert len(resources) == 2 and all(row[1] == 0 for row in resources), resources
            home_resource = next(row for row in resources if row[0] == "agent-home:worker")
            assert '"status": "cleaned"' in home_resource[2], home_resource
            receipts = db.execute(
                "SELECT resource_key, retired FROM lifecycle_receipts "
                "WHERE resource_key IN ('agent-record:worker', 'agent-home:worker')"
            ).fetchall()
            assert all(row[1] == 1 for row in receipts), receipts
        after = hosted.request(self.socket, "ps", {})
        assert not any(
            row.get("uri") == uri and row.get("status") != "offline"
            for row in after.get("agents", [])
        ), after
        with sqlite3.connect(self.root / "state/lifecycle-operations.sqlite3") as db:
            assert not db.execute(
                "SELECT 1 FROM harnesses WHERE name='worker'"
            ).fetchone(), "destroy left a durable harness row"
        replacement = self.cli("agent", "create", "--name", "worker")["agent"]
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            new_token = db.execute(
                "SELECT entity_token FROM agents WHERE actor='worker'"
            ).fetchone()[0]
        assert new_token != old_token
        assert self.cli("agent", "grant", "worker", "list")["grants"] == []
        base.write_json(
            self.root / "destroy-evidence.json",
            {"response": result, "uri": uri, "replacement": replacement},
        )


class RollbackScenario(base.Scenario):
    scope = "AT04 isolated old/new builds, P4 backup rollback (the only supported downgrade); L0 only"

    def use_build(self, tree):
        assert self.process is None
        self.python = str(tree / ".venv/bin/python")
        assert Path(self.python).exists(), "old and new checkouts each need a venv"
        self.env.update(PYTHONPATH=str(tree / "src"), AT01_SOURCE=str(tree))

    def save(self, label):
        """Consistent copy of every store P4 rewrites, laid out like the root."""
        assert self.process is None, "backup requires stopped writers"
        directory = self.root / label
        (directory / "state").mkdir(parents=True)
        shutil.copytree(self.root / "home", directory / "home")
        for name in RESTORED:
            if not (self.root / "state" / name).exists():
                continue
            source = sqlite3.connect(self.root / "state" / name)
            dest = sqlite3.connect(directory / "state" / name)
            try:
                source.backup(dest)
            finally:
                dest.close()
                source.close()
        assert base.snapshot(directory) == base.snapshot(self.root), f"{label}: backup differs from its source"
        return directory

    def restore(self, directory):
        assert self.process is None, "restore requires stopped writers"
        # Every path belongs to this scenario's freshly allocated temp root.
        shutil.rmtree(self.root / "home")
        shutil.copytree(directory / "home", self.root / "home")
        for name in RESTORED:
            for suffix in ("", "-wal", "-shm"):
                (self.root / "state" / (name + suffix)).unlink(missing_ok=True)
            if (directory / "state" / name).exists():
                shutil.copy2(directory / "state" / name, self.root / "state" / name)
        assert base.snapshot(self.root) == base.snapshot(directory), "restore differs from its backup"

    def settled_snapshot(self):
        """The new build's snapshot once its own user agent (UA1b) settled, with that agent split out."""
        return base.user_agent.settled_snapshot(
            self.socket, base.snapshot, self.root, "host-owner", "host-node", base.agent_id_for)

    def run(self):
        old = self.old_tree
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=old, text=True).strip() == OLD_SHA
        assert not subprocess.check_output(["git", "diff", "HEAD"], cwd=old), "old source is dirty"
        worker_root = self.root / "worker-journey"
        worker_root.mkdir()
        worker = DestroyScenario(worker_root, self.production_socket)
        try:
            worker.run()
        finally:
            worker.stop()
        self.use_build(old)
        setup = subprocess.run([self.python, "-c", SEED], cwd=old, env=self.env,
                               text=True, capture_output=True, timeout=30)
        base.write_json(self.root / "old-seed.json", {"head": OLD_SHA, "exitCode": setup.returncode,
                        "stdout": setup.stdout, "stderr": setup.stderr, "source": SEED})
        assert setup.returncode == 0, setup.stderr
        self.start()
        original = base.snapshot(self.root)
        original_list = self.cli("agent", "list")["agents"]
        self.stop()
        backup = self.save("before-upgrade")
        self.use_build(base.ROOT)
        self.start()
        upgraded = self.settled_snapshot()
        assert upgraded == base.after_p4(original), \
            "upgrade changed persistent rows or home beyond the P4 migration"
        # P4 is one-way (U1): every stored name-form uri is now in id form.
        migrated = {row[0]: row[3] for row in upgraded["rows"]["agents"]}
        assert all(ID_FORM_URI.fullmatch(uri) for uri in migrated.values()), migrated
        assert not any(ID_FORM_URI.fullmatch(agent["uri"]) for agent in original_list), original_list
        plant_legacy_foreign_hosted_row(self.root)
        self.stop()
        candidate = self.save("before-bad-downgrade")
        # Unsupported downgrade (ruling 2026-11-07): the old build on the
        # migrated state is started only to witness that it fails closed.
        self.use_build(old)
        try:
            self.start()
        except AssertionError:
            assert self.process is not None and self.process.poll() not in (None, 0), "not a target startup rejection"
            code = self.process.returncode
            self.process = None
            error = (self.root / f"daemon-{self.generation}.stderr").read_text()
            output = (self.root / f"daemon-{self.generation}.stdout").read_text()
            refusal = json.loads(output)
            assert refusal["code"] == "OWNER_MIGRATION_CUSTODY_CONFLICT", refusal
            assert refusal["data"]["old"] == "visitor@example.test", refusal
            base.write_json(self.root / "direct-downgrade.json", {"exitCode": code, "stdout": output, "stderr": error})
        else:
            raise AssertionError("old daemon accepted a remaining foreign-owner hosted row")
        self.restore(candidate)
        self.use_build(base.ROOT)
        self.start()
        # Backup rollback restores every store P4 rewrote plus home; the
        # message stores retain current data. Explicitly witness an orphaned
        # pending message.
        late = self.cli("agent", "create", "--name", "late-native")["agent"]["uri"]
        self.ensure_org_space("native", prefix="hosted-upgrade")
        self.cli(
            "message", "send", "--from", "native", "--to", late,
            "--summary", "AT04_PENDING_SENTINEL", "AT04_PENDING_SENTINEL",
        )
        deadline = time.monotonic() + 45
        inbox = None
        while time.monotonic() < deadline:
            inbox = base.request(self.socket, "message.query", {"actor": late, "view": "inbox"})
            if "AT04_PENDING_SENTINEL" in json.dumps(inbox):
                break
            time.sleep(0.25)
        else:
            raise AssertionError(
                f"AT04_PENDING_SENTINEL never reached the late agent's inbox: {inbox}"
            )
        self.stop()
        # The state being rolled back really holds addresses the old build
        # cannot route, so the scan below is not vacuous.
        before_rollback = id_form_uris(self.root)
        assert set(migrated.values()) | {late} <= before_rollback, before_rollback
        self.save("new-state-retained-for-recovery")
        self.restore(backup)
        assert not id_form_uris(self.root), "the restored backup still holds id-form addresses"
        self.use_build(old)
        self.start()
        assert self.cli("agent", "list")["agents"] == original_list
        assert base.snapshot(self.root) == original
        assert not id_form_uris(self.root), "the old build reads id-form addresses after backup rollback"
        orphan = base.request(self.socket, "message.query", {"actor": late, "view": "inbox"})
        assert "AT04_PENDING_SENTINEL" in json.dumps(orphan), orphan
        assert not any(a["uri"] == late for a in original_list)
        self.stop()
        # The restored backup is a complete pre-P4 state: upgrading again
        # repeats exactly the documented P4 rewrite and mints the same ids.
        rolled_back = base.snapshot(self.root)
        self.use_build(base.ROOT)
        self.start()
        again = self.settled_snapshot()
        assert again == base.after_p4(rolled_back), \
            "re-upgrade after backup rollback changed rows or home beyond the P4 migration"
        assert {row[0]: row[3] for row in again["rows"]["agents"]} == migrated
        base.write_json(self.root / "rollback-evidence.json", {
            "oldHead": OLD_SHA, "original": original, "originalAgents": original_list,
            "supportedDowngrade": "backup rollback only (Allen 2026-11-07); clean-row rollback unsupported after P4",
            "backupRollback": "PASS", "reupgradeAfterRollback": "PASS", "orphanQuery": orphan,
            "idFormBeforeRollback": sorted(before_rollback),
            "restored": ["home/**", *RESTORED], "otherState": "retained; orphan is not a running agent",
        })

    def __init__(self, root, production_socket):
        super().__init__(root, production_socket)
        self.production_socket = production_socket


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--old-tree", type=Path, required=True)
    args, remaining = parser.parse_known_args()
    RollbackScenario.old_tree = args.old_tree.resolve()
    sys.argv = [sys.argv[0], *remaining]
    raise SystemExit(base.main(RollbackScenario))
