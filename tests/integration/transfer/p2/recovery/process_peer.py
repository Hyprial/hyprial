"""Synthetic protocol peer: real process/SQLite, no daemon or model account.

Parent controls every boundary over pipes. File markers represent local effects,
not worker liveness; this does not certify daemon restart/strict-resume behavior.
"""

from dataclasses import asdict
import json
from pathlib import Path
import sys

from hyprial.daemon.impl.state_db import StateDatabase
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Binding, Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore


def emit(**data):
    print(json.dumps(data), flush=True)


def main():
    root, role, operation = Path(sys.argv[1]), Role(sys.argv[2]), sys.argv[3]
    binding = Binding(role, "test-owner", "source", "a.synthetic", "source-incarnation",
                      "target", "visitor", "a" * 64)
    store = AdmissionStore(StateDatabase(root / "lifecycle-operations.sqlite3"),
                           node=role.value, owner="test-owner")
    journal = getattr(store, "journal", None)
    emit(event="ready", supported=journal is not None)
    if journal is None:
        return
    actor = "worker" if role is Role.SOURCE else "visitor"
    for line in sys.stdin:
        request = json.loads(line)
        command = request["command"]
        try:
            if command == "begin":
                store.prepare(operation, binding)
                start = journal.begin(operation, step=request["step"],
                                      fingerprint=request["fingerprint"], actor=actor)
                emit(event="intent", execute=start.execute, state=asdict(start.state))
            elif command == "effect":
                with (root / f"{operation}.effect").open("a") as stream:
                    stream.write("synthetic-effect\n")
                    stream.flush()
                emit(event="effect")
            elif command == "finish":
                state = journal.get(operation)
                result = journal.finish(operation, expected=state, receipt=request["receipt"],
                                        entity_token=request["token"])
                emit(event="finished", state=asdict(result))
            elif command == "read":
                state = journal.get(operation)
                emit(event="state", state=asdict(state) if state else None,
                     blocked=journal.custody("worker", "source-incarnation").blocked,
                     replacement_blocked=journal.custody("worker", "new-incarnation").blocked)
            elif command == "override":
                state = journal.get(operation)
                result = journal.attention(operation, expected=state, event_id="manual-1",
                                           principal="synthetic-operator", action="manual-restore")
                emit(event="attention", state=asdict(result),
                     audit=[asdict(e) for e in journal.events(operation)])
            elif command == "audit":
                emit(event="audit", audit=[asdict(e) for e in journal.events(operation)])
            elif command == "cancel":
                store.cancel_prepared(operation)
                emit(event="cancelled")
            else:
                raise ValueError(command)
        except AdmissionConflict as error:
            emit(event="rejected", reason=error.reason.value)


if __name__ == "__main__":
    main()
