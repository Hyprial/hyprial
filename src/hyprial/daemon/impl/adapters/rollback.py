"""Offline, fenced restoration of the first pre-adapter-split document."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from hyprial.kernel import DesiredStateError

BACKUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapter_model_backup (
    id INTEGER PRIMARY KEY CHECK(id=1),
    schema_version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    document_json TEXT NOT NULL
);
"""


def backup_document(db, document):
    if document is None or document["schemaVersion"] >= 3:
        return
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    db.execute("INSERT OR IGNORE INTO adapter_model_backup VALUES(1,?,?,?)",
               (document["schemaVersion"], hashlib.sha256(text.encode()).hexdigest(), text))


def _semantic_intent(state):
    """Ignore only known launch-codec representation changes, not intent.

    Keep persisted legacy metadata and the immutable backup untouched. Unknown
    fields stay byte-distinct after JSON encoding, including null/default-like
    values; they are not part of the historical launch codec's default rules.
    """
    defaults = {
        "args": [], "ownership": "managed", "headless": True, "command": [],
        "containerized": False, "nickname": None, "cwd": None, "endpoint": None,
        "sessionRef": None, "turnTimeoutSeconds": None, "idleTimeoutSeconds": None,
        "pinnedOwner": None, "containerImage": None, "executionRuntime": None,
        "modelProvider": None, "model": None,
    }
    adapters = []
    for adapter in state.adapters:
        metadata = json.loads(adapter.legacy_launch)
        for key, default in defaults.items():
            if key in metadata and type(metadata[key]) is type(default) and metadata[key] == default:
                del metadata[key]
        for key in ("turnTimeoutSeconds", "idleTimeoutSeconds"):
            if type(metadata.get(key)) in (int, float):
                metadata[key] = float(metadata[key])
        adapters.append(replace(adapter, legacy_launch=json.dumps(
            metadata, sort_keys=True, separators=(",", ":"),
        )))
    return replace(state, adapters=tuple(adapters))


def downgrade_adapter_state(state_dir: Path) -> dict[str, object]:
    """Restore only under the daemon fence, and only without intervening intent."""
    from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence
    from hyprial.daemon.impl.desired_state.documents import DesiredState
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
    from hyprial.daemon.impl.state_db import StateDatabase

    try:
        state_dir = state_dir.resolve(strict=True)
    except FileNotFoundError as error:
        raise DesiredStateError("adapter downgrade requires a migrated state directory") from error
    with DaemonStateOwnershipFence.acquire(state_dir, timeout=0.0):
        database = StateDatabase(state_dir / "lifecycle-operations.sqlite3")
        if not database.exists():
            raise DesiredStateError("adapter downgrade requires a migrated state database")
        shadow = DesiredStateSqliteShadow(database)
        # Do not construct DesiredStateStore: that would migrate the restored
        # schema back to 3. The writer fence also excludes another offline tool.
        with database.transaction() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "adapter_model_backup" not in tables:
                raise DesiredStateError("pre-migration adapter backup is absent")
            row = db.execute("SELECT schema_version,sha256,document_json FROM adapter_model_backup WHERE id=1").fetchone()
            if row is None:
                raise DesiredStateError("pre-migration adapter backup is absent")
            version, digest, text = row
            if hashlib.sha256(text.encode()).hexdigest() != digest:
                raise DesiredStateError("pre-migration adapter backup SHA256 mismatch")
            document = json.loads(text)
            if version not in (1, 2) or document.get("schemaVersion") != version:
                raise DesiredStateError("pre-migration adapter backup version mismatch")
            migrated = DesiredState.from_json(document)
            current = shadow.read_document()
            if _semantic_intent(DesiredState.from_json(current)) != _semantic_intent(migrated):
                raise DesiredStateError("desired state changed after migration; refusing to rewind intent")
            if current["schemaVersion"] != 2:
                # SQLite's legacy columns require normalized defaults. Workers
                # use their codec; adapter launch metadata is restored below.
                legacy = migrated.to_json()
                legacy["schemaVersion"] = 2
                legacy.pop("adapters")
                for adapter in migrated.adapters:
                    metadata = json.loads(adapter.legacy_launch)
                    metadata.setdefault("args", [])
                    metadata.setdefault("ownership", "managed")
                    metadata.setdefault("headless", True)
                    legacy["providers"].append({**metadata, "provider": adapter.provider,
                                                "name": adapter.name, "status": adapter.status})
                shadow._delete_all(db)
                shadow._insert_all(db, legacy)
        return {"ok": True, "schemaVersion": 2, "backupSha256": digest,
                "adapters": len(migrated.adapters),
                "nextStep": "Install the older binary before starting the daemon."}
