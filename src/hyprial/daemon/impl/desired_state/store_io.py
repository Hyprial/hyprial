"""DesiredStateStore storage IO cluster: sqlite import migration and atomic JSON document IO."""

from __future__ import annotations

from .documents import DesiredState
import json
import os
import tempfile
import time
from pathlib import Path
from hyprial.kernel import DesiredStateError  # canonical defs (MP-1)

class _DesiredStateStoreIoMixin:
    """DesiredStateStore cluster; the composing class owns the state."""

    def _migrate_into_sqlite(self) -> None:
        """Absorb a pre-SQLite home -- Allen's ruling, one rule:

        At construction: if the canonical ``.v1`` document exists, import
        it in ONE transaction (replace-all; re-importing the same data is
        lossless, so this is idempotent) and then archive the file as
        ``.v1.migrated-<ts>`` (kept, not deleted).  If it does not exist,
        do nothing.

        Two properties here are pinned, not decoration (hyprial-developer
        review):

        - The import reads ONLY the ``.v1`` file; ``desired-state.json``
          is never looked at.  On every real upgraded machine that file
          holds the migration.py poison pill (marker
          ``__hyprial_desired_state_schema_v1__``) -- reading it would import
          a harness named "rollback-blocked".
        - The archive rename is FATAL on failure (nothing catches it).
          Re-import being lossless relies on the .v1 being gone once
          imported: a rename failure left in place would make the next
          start replace newer SQLite state with the stale document,
          silently.  A crash between the commit and the rename is the
          benign variant -- the next start re-imports the same data and
          the rename succeeds.
        """

        if not self.versioned_path.exists():
            return
        document = self._read_import_document()
        self._sqlite_shadow.write_document(document)
        self.versioned_path.rename(self._archive_path(self.versioned_path))

    def _read_import_document(self) -> dict[str, object]:
        """Parse the canonical ``.v1`` document; loud on a broken file.

        Reads ONLY the ``.v1`` file -- never ``desired-state.json`` (see
        the poison-pill note in ``_migrate_into_sqlite``).
        """

        raw = self._read_json(self.versioned_path)
        try:
            return DesiredState.from_json(raw).to_json()
        except DesiredStateError as error:
            raise DesiredStateError(
                f"cannot import desired state from {self.versioned_path}: {error}"
            ) from error

    @staticmethod
    def _archive_path(path: Path) -> Path:
        stamp = int(time.time())
        candidate = Path(f"{path}.migrated-{stamp}")
        counter = 0
        while candidate.exists():
            counter += 1
            candidate = Path(f"{path}.migrated-{stamp}-{counter}")
        return candidate

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DesiredStateError(
                f"cannot read desired state {path}: {error}"
            ) from error

    @staticmethod
    def _atomic_json_write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
