"""Explicit offline rollback of new runtime intent, never an automatic downgrade."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from hyprial.kernel import has_execution_runtime
from hyprial.daemon.impl.desired_state import DesiredState, DesiredStateStore
from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence


class RuntimeDowngradeError(ValueError):
    code = "RUNTIME_DOWNGRADE_REFUSED"


def downgrade_state(state_dir: Path, output: Path) -> dict[str, object]:
    """Retire only smolvm intent under the existing daemon ownership flock.

    Terminal journal rows stay byte-for-byte historical. No forward or undo
    cursor may remain. Never change Agent, sessions, home data or grants.
    """
    state_dir = state_dir.resolve(strict=True)
    output = output.absolute()
    if output.exists() or output.parent.resolve(strict=True) != output.parent:
        raise RuntimeDowngradeError("Export must be a new canonical directory")
    with DaemonStateOwnershipFence.acquire(state_dir):
        live = state_dir / "smolvm-runtimes"
        if live.is_symlink() or (live.exists() and any(live.iterdir())):
            raise RuntimeDowngradeError(
                "Owned smolvm resources remain; stop/fence them with the new runtime first"
            )
        store = DesiredStateStore(state_dir / "desired-state.json")
        state = store.load()
        if state.schema_version != 2:
            raise RuntimeDowngradeError("No schema 2 runtime state to downgrade")
        removed = tuple(s for s in state.harnesses if s.execution_runtime is not None)
        resources = tuple(
            r for r in state.lifecycle_resources if has_execution_runtime(r.payload)
        )
        keys = {r.resource_key for r in resources}
        keys.update(f"harness:{s.harness}:{s.name}" for s in removed)
        receipts = tuple(r for r in state.lifecycle_receipts if r.resource_key in keys)
        if any(not r.retired or not r.completed for r in receipts):
            raise RuntimeDowngradeError(
                "Runtime lifecycle receipts have not been retired"
            )
        remaining = replace(
            state,
            schema_version=1,
            harnesses=tuple(s for s in state.harnesses if s.execution_runtime is None),
            lifecycle_resources=tuple(
                r for r in state.lifecycle_resources if r not in resources
            ),
            lifecycle_receipts=tuple(
                r for r in state.lifecycle_receipts if r not in receipts
            ),
        )
        document = remaining.to_json()
        DesiredState.from_json(document)  # No unknown executable runtime references.
        with store.state_db.transaction() as db:
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            historical = []
            if "lifecycle_operations" in tables:
                operations = db.execute(
                    "SELECT operation_id, request_json, state FROM lifecycle_operations"
                ).fetchall()
                if any(r[2] not in ("completed", "compensated") for r in operations):
                    raise RuntimeDowngradeError(
                        "Unsettled or unknown lifecycle operation; finish compensation first"
                    )
                historical = [
                    dict(operationId=r[0], state=r[2], request=json.loads(r[1]))
                    for r in operations
                    if has_execution_runtime(json.loads(r[1]))
                ]
                if "lifecycle_effects" in tables and any(
                    db.execute(
                        "SELECT 1 FROM lifecycle_effects WHERE operation_id=? AND receipt_retired=0 LIMIT 1",
                        (row["operationId"],),
                    ).fetchone()
                    for row in historical
                ):
                    raise RuntimeDowngradeError("Runtime effects are not fully retired")
            # Non-secret execution intent only. Identity/secret stores are never opened.
            export = dict(
                schemaVersion=2,
                removed=[s.to_json() for s in removed],
                resources=[r.to_json() for r in resources],
                receipts=[r.to_json() for r in receipts],
                retainedTerminalHistory=historical,
            )
            output.mkdir(mode=0o700)
            path = output / "runtime-intent.json"
            fd = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                json.dump(export, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            for directory in (output, output.parent):
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            # Same transaction: delete only new executable intent/references and root2.
            # This deliberately bypasses sticky normal-save promotion, under the lease.
            for spec in removed:
                db.execute(
                    "DELETE FROM harnesses WHERE harness=? AND name=?",
                    (spec.harness, spec.name),
                )
            for resource in resources:
                db.execute(
                    "DELETE FROM lifecycle_resources WHERE domain=? AND resource_key=?",
                    (resource.domain, resource.resource_key),
                )
            for receipt in receipts:
                db.execute(
                    "DELETE FROM lifecycle_receipts WHERE domain=? AND attempt_token=?",
                    (receipt.domain, receipt.attempt_token),
                )
            db.execute(
                "UPDATE root_state SET schema_version=1 WHERE id=1 AND schema_version=2"
            )
        if store.load().to_json() != document:
            raise RuntimeDowngradeError(
                "Post-commit verification mismatch; retain export and do not start old binary"
            )
        return {
            "ok": True,
            "schemaVersion": 1,
            "removed": len(removed),
            "export": str(path),
            "retainedTerminalHistory": len(historical),
        }
