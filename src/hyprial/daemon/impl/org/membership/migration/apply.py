# Decision record
# - 2026-11-24 (S3a fix, coordinator ruling): a journal that every inventory
#   entity finished (phase REPORTED) under an older build differs from today's
#   inventory only by the retired ``receivers`` key on user rows.  The drift
#   fence compares the frozen inventory without that key, so a completed node
#   stays done on every rerun.  An unfinished journal keeps the source-drift
#   fence and the manual step in docs/guides/migration-remove-squire.md.
"""Entity-level recovery over the live facade; no alternate registry or replica.

Inventory and intended records precede writes. Existing records are never
replaced. Readback versions fence later retries against revocation and drift.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.org.membership.store import OrgMembershipError, OrgMembershipStore
from hyprial.daemon.impl.org.network.directory import InvalidDeviceDiagnostics, OrgFsDirectoryStore
from hyprial.daemon.impl.org.store import OrgStoreError
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.kernel import is_agent_id_segment

from .inventory import agent_address_matches, canonical_owner, resident_targets
from .journal import Journal, digest, new_report, write_report

_PHASES = ("INVENTORY", "PERSONAL_READY", "MEMBERSHIPS_WRITTEN", "VERIFIED", "REPORTED")


class Pending(Exception):
    pass


class Conflict(Exception):
    def __init__(self, reason: str, **details):
        super().__init__(reason)
        self.details = details



class MigrationService:
    def __init__(self, *, fs, home: Path, owner: str, node_id: str,
                 inventory: Callable,
                 invalid_devices: InvalidDeviceDiagnostics | None = None,
                 log: Callable | None = None):
        self.fs = fs
        self.home = Path(home)
        self.owner = canonical_owner(owner)
        self.node_id = node_id
        self.inventory = inventory
        self.store = OrgMembershipStore(fs)
        self.directory = OrgFsDirectoryStore(
            fs, invalid_devices=invalid_devices or InvalidDeviceDiagnostics(), logger=log
        )
        self.journal = None

    def checkpoint(self, phase: str, entity: str | None = None) -> None:
        if entity is not None:
            state = self.journal.state(entity)
            if _PHASES.index(state["phase"]) < _PHASES.index(phase):
                state["phase"] = phase
        self.journal.save()

    def run(self) -> dict:
        report = new_report(self.node_id)
        try:
            source = self.inventory()
            report["sourceDigest"] = digest(source)
            self.journal = Journal(self.home, source)
        except (OrgFsError, OrgMembershipError, OrgStoreError, ValueError, OSError) as error:
            report["failed"].append({"entity": "inventory", "reason": getattr(error, "code", type(error).__name__)})
            write_report(self.home, report)
            return report
        if (self.journal.data["sourceDigest"] != digest(source)
                and not self._completed_before_receivers_retired(source)):
            report["failed"].append({"entity": "inventory", "reason": "source-drift"})
            self.journal.report(report)
            return report
        self.checkpoint("INVENTORY")
        targets, problems = resident_targets(source)
        report["pending"].extend(problems)
        for user in source["users"]:
            self._entity(report, "user:" + user["owner"], lambda user=user: self._user(user))
        for agent in source["agents"]:
            self._entity(report, agent["agent"], lambda agent=agent: self._agent(agent, targets.get(agent["agent"], [])))
        report["verified"] = not (report["failed"] or report["pending"] or report["conflicts"] or report["unconfirmed"])
        self.journal.report(report)
        self.checkpoint("REPORTED")
        for row in report["entities"]:
            if row["verified"]:
                self.checkpoint("REPORTED", row["entity"])
        return report

    def _completed_before_receivers_retired(self, source: dict) -> bool:
        """Whether a finished journal drifts only by the retired receivers key."""

        data = self.journal.data
        inventory = data.get("inventory")
        try:
            if digest(inventory) != data["sourceDigest"]:
                return False
            users = [{key: value for key, value in row.items() if key != "receivers"}
                     for row in inventory["users"]]
            if digest({**inventory, "users": users}) != digest(source):
                return False
            entities = ["user:" + row["owner"] for row in users]
            entities += [row["agent"] for row in inventory["agents"]]
            states = data["entities"]
            return all(states.get(entity, {}).get("phase") == "REPORTED" for entity in entities)
        except (AttributeError, KeyError, TypeError):
            return False

    def _entity(self, report: dict, entity: str, apply: Callable) -> None:
        state = self.journal.state(entity)
        row = {"entity": entity, "sourceDigest": report["sourceDigest"], "phase": state["phase"],
               "targets": [], "verified": False, "state": "pending"}
        try:
            apply()
            self.checkpoint("VERIFIED", entity)
            row.update(verified=True, state="done")
        except Pending as error:
            report["pending"].append({"entity": entity, "reason": str(error)})
        except Conflict as error:
            row["state"] = "conflict"
            problem = {"entity": entity, "reason": str(error), **error.details}
            report["conflicts"].append(problem)
            report["failed"].append(problem)
        except (OrgFsError, OrgMembershipError, ValueError, OSError) as error:
            row["state"] = "failed"
            report["failed"].append({"entity": entity, "reason": getattr(error, "code", type(error).__name__)})
        row["phase"] = state["phase"]
        row["targets"] = sorted(state["targets"])
        for target, plan in state["targets"].items():
            if row["verified"] and plan.get("confirmationBasis"):
                row["confirmationBasis"] = plan["confirmationBasis"]
            if plan.get("version"):
                report["created" if plan["created"] else "reused"].append({"entity": entity, "target": target})
        report["entities"].append(row)

    def _space(self, org: str):
        space = self.store.space_for(org)
        if space is None:
            raise Pending("org-not-synced")
        if self.directory.org_state(org) != "active":
            raise Pending("org-not-active")
        return space

    def _user(self, user: dict) -> None:
        owner = canonical_owner(user["owner"])
        entity = "user:" + owner
        self.journal.state(entity)
        self.checkpoint("PERSONAL_READY", entity)

    def _agent(self, agent: dict, context_orgs: list[str]) -> None:
        owner = canonical_owner(agent["owner"])
        entity = agent["agent"]
        if not agent_address_matches(entity, owner, agent["node"], agent["name"]):
            raise Conflict("agent-identity-conflict")
        if not is_agent_id_segment(agent["agentId"]):
            raise Conflict("agent-identity-conflict")
        state = self.journal.state(entity)
        if not state["targets"]:
            state["targets"] = {}
            self.journal.save()
        for org in sorted(set(context_orgs)):
            self._space(org)
        self.checkpoint("MEMBERSHIPS_WRITTEN", entity)

