#!/usr/bin/env python3
"""AT02: capability grant and incarnation lifecycle for a local agent."""

import importlib.util
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "hosted_registry", Path(__file__).with_name("run-hosted-registry.py")
)
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class LedgerScenario(base.Scenario):
    scope = "local-owner capability ledger lifecycle; L0 only"

    def run(self):
        self.start()
        created = self.cli("agent", "create", "--name", "ledger")["agent"]
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            first_token = db.execute(
                "SELECT entity_token FROM agents WHERE actor = 'ledger'"
            ).fetchone()[0]
        assert created["owner"] == "host-owner"
        assert self.cli("agent", "grant", "ledger", "list", "--audit")["journal"] == []
        first = self.cli(
            "agent", "grant", "ledger", "--capability", "send-to",
            "--scope", '["user:visitor"]',
        )["grant"]
        grant_id = first["grantId"]
        assert first["revision"] == 1
        self.cli(
            "agent", "grant", "ledger", "--capability", "send-to",
            "--scope", '["user:visitor"]', "--grant-id", grant_id, ok=False,
        )
        revised = self.cli(
            "agent", "grant", "ledger", "--capability", "send-to",
            "--scope", '["agent:visitor:node:helper"]', "--grant-id", grant_id,
            "--revision", "2",
        )["grant"]
        self.stop()
        self.start()
        assert self.cli("agent", "grant", "ledger", "list")["grants"] == [revised]
        assert self.cli("agent", "grant", "ledger", "revoke", grant_id)["revoked"]
        assert self.cli("agent", "grants")["grants"] == []
        before_destroy = self.cli("agent", "grant", "ledger", "list", "--audit")["journal"]
        assert self.cli("agent", "destroy", "ledger", "--yes")["destroyed"]
        assert self.cli("agent", "grant", "ledger", "list", "--audit")["journal"] == before_destroy
        replacement = self.cli("agent", "create", "--name", "ledger")["agent"]
        with sqlite3.connect(self.root / "state/agents.sqlite3") as db:
            second_token = db.execute(
                "SELECT entity_token FROM agents WHERE actor = 'ledger'"
            ).fetchone()[0]
        assert second_token != first_token
        assert self.cli("agent", "grant", "ledger", "list")["grants"] == []
        base.write_json(
            self.root / "final-journal.json",
            {"first": first, "revised": revised, "replacement": replacement},
        )


if __name__ == "__main__":
    raise SystemExit(base.main(LedgerScenario))
