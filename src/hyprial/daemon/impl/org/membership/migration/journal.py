"""Freeze sanitized inventory before writes; completion never authorizes replay."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from hyprial.kernel import atomic_json_write, harden_private_path


def new_report(node_id: str) -> dict:
    return {"schemaVersion": 1, "operationId": uuid4().hex, "node": node_id,
            "sourceDigest": None, "entities": [], "created": [], "reused": [],
            "conflicts": [], "failed": [], "pending": [], "unconfirmed": [],
            "state": "pending", "verified": False}


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Journal:
    def __init__(self, home: Path, source: dict) -> None:
        self.root = home / "state" / "org-membership-v1"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        harden_private_path(self.root, mode=0o700)
        self.path = self.root / "journal.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict) or self.data.get("schemaVersion") != 1:
                raise ValueError("invalid migration journal")
        else:
            self.data = {"schemaVersion": 1, "inventory": source, "sourceDigest": digest(source),
                         "startedAt": datetime.now(UTC).isoformat(), "entities": {}}
            self.save()

    def save(self) -> None:
        atomic_json_write(self.path, self.data)
        harden_private_path(self.path, mode=0o600)

    def state(self, entity: str) -> dict:
        return self.data["entities"].setdefault(entity, {"phase": "INVENTORY", "targets": {}})

    def report(self, report: dict) -> None:
        write_report(self.root.parent.parent, report)


def write_report(home: Path, report: dict) -> None:
    report["state"] = ("conflict" if report["conflicts"] else "failed" if report["failed"]
                       else "applied_unconfirmed" if report["unconfirmed"]
                       else "pending" if report["pending"] or not report["verified"] else "done")
    root = home / "state" / "org-membership-v1"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    harden_private_path(root, mode=0o700)
    path = root / f"report-{uuid4().hex}.json"
    report["reportPath"] = str(path)
    atomic_json_write(path, report)
    harden_private_path(path, mode=0o600)
