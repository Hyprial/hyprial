"""Local invite operation checkpoints, not a membership authority.

Only read-back OrgFS state proves membership. No bearer enters this journal.
The existing Tailnet redemption journal retains its remote idempotency key.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.forwarding.membership.settings import durable_json_write


class AcceptJournal:
    def __init__(self, home: Path, identifier: str) -> None:
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        self.path = Path(home) / "org-invites" / (digest + ".json")
        self.identifier = identifier
        self.record = self.read()

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if (not isinstance(value, dict) or value.get("schema") != 1
                or value.get("inviteId") != self.identifier
                or not isinstance(value.get("completed"), list)):
            raise ValueError("invalid invite acceptance checkpoint")
        return value

    def start(self) -> None:
        if not self.record:
            self.record = {"schema": 1, "inviteId": self.identifier,
                           "state": "started", "completed": [], "steps": []}
            self.save()

    def save(self) -> None:
        durable_json_write(self.path, self.record)

    def begin(self, step: str) -> None:
        self.record.update(state="partial", missingStep=step)
        self.save()

    def complete_step(self, step: str) -> None:
        if step not in self.record["completed"]:
            self.record["completed"].append(step)
        self.record.update(state=step)
        self.record.pop("missingStep", None)
        self.save()

    def done(self, result: dict[str, Any]) -> None:
        self.record.update(state="done", result=result)
        self.record.pop("missingStep", None)
        self.save()
