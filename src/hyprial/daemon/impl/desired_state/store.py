"""DesiredStateStore core: load/save, runtime schema gate and the harness lifecycle journal operations."""

from __future__ import annotations
from hyprial.daemon.impl.desired_state.documents import (
    _apply_create_resource,
    _apply_delete_resource,
    _new_stored_receipt,
    _receipt_by_attempt,
    _reconcile_resource,
    _resource_map,
    _stored_receipt,
    _with_external_resource,
)

import sqlite3
import threading
import weakref
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from hyprial.daemon.impl.desired_state.sqlite  import DesiredStateSqliteShadow
from hyprial.daemon.impl.state_db  import StateDatabase
from hyprial.kernel  import (
    MutationProvenance,
    )
from hyprial.daemon.impl.lifecycle.lifecycle_receipts  import (
    StoredLifecycleReceipt,
)
from hyprial.kernel import DomainEffectClaim
from hyprial.kernel import LifecycleMutationRequest
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec  # canonical defs (MP-1)
from hyprial.kernel import has_execution_runtime

from .documents import (
    DesiredState,
    SUPPORTED_SCHEMA_VERSIONS,
)
from .sessions import _DesiredStateSessionsMixin
from .store_io import _DesiredStateStoreIoMixin
from .services import ServiceRegistry


_STORES_BY_DB_PATH: "weakref.WeakValueDictionary[Path, DesiredStateStore]" = (
    weakref.WeakValueDictionary()
)


_STORE_REGISTRY_LOCK = threading.Lock()


def _register_store(store: "DesiredStateStore") -> None:
    key = store.state_db.path.resolve()
    with _STORE_REGISTRY_LOCK:
        existing = _STORES_BY_DB_PATH.get(key)
        if existing is not None and existing is not store and (
            existing.legacy_path != store.legacy_path
        ):
            raise DesiredStateError(
                "two desired-state stores with different documents share one "
                f"state directory ({existing.legacy_path} and {store.legacy_path}); "
                "the SQLite authority is one document per state dir -- give this "
                "store its own state directory"
            )
        _STORES_BY_DB_PATH[key] = store


class DesiredStateStore(_DesiredStateSessionsMixin, _DesiredStateStoreIoMixin):

    def __init__(self, legacy_path: Path, state_db: StateDatabase | None = None) -> None:
        self.legacy_path = Path(legacy_path)
        self.versioned_path = Path(f"{legacy_path}.v1")
        self._lock = threading.RLock()
        # U0a-2: the shared state database.  Same file the lifecycle journal
        # uses; when the caller (DaemonApplication) passes one instance, the
        # desired-state store and the journal write through the SAME
        # serialized connection owner, so in-process write contention
        # disappears by construction.  ``None`` (every standalone
        # construction, all tests) means: own the derived path.
        # File creation stays lazy: read-only stores leave no new files.
        self.state_db = (
            state_db
            if state_db is not None
            else StateDatabase(self.legacy_path.parent / "lifecycle-operations.sqlite3")
        )
        self._sqlite_shadow = DesiredStateSqliteShadow(self.state_db)
        # The SQLite authority is ONE document per state dir.  Two stores
        # pointing at different documents in the same directory would
        # silently clobber each other's committed state -- refuse loudly.
        _register_store(self)
        # Absorb homes from earlier builds (or finish an interrupted
        # migration) before the first read.
        with self._lock:
            self._migrate_into_sqlite()
            # U0b: add the harnesses.status column (and resolve rows whose
            # last lifecycle attempt never settled) on pre-U0b databases.
            # Guarded by file existence so read-only stores still leave no
            # new files behind.
            if self.state_db.exists():
                self._sqlite_shadow.migrate_harness_status()
                self._sqlite_shadow.migrate_interactive_process_identity()

    def harness_spec(
        self, name: str, *, excluding: str | None = None
    ) -> HarnessLaunchSpec | None:
        """One harness row by name (the port's single-row read, same contract).

        The file store has no published projection to read from, so this
        reads the document once; the daemon's port serves it from memory.
        """

        return next(
            (
                item
                for item in self.load().harnesses
                if item.name == name and item.harness != excluding
            ),
            None,
        )

    def load(self) -> DesiredState:
        """Read the desired state from SQLite (the only storage).

        The document written by the last committed transaction is the
        authority.  ``None`` here means a genuinely fresh home: the
        migration ran at construction and found nothing to import.
        """

        with self._lock:
            document = self._sqlite_shadow.read_document()
            if document is None:
                return DesiredState()
            return DesiredState.from_json(document)

    def save(self, state: DesiredState) -> None:
        if type(state.schema_version) is not int or state.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise DesiredStateError(
                f"unsupported desired-state schema version {state.schema_version!r}"
            )
        with self._lock:
            payload = state.to_json()
            if has_execution_runtime(payload):
                payload["schemaVersion"] = 2
            validated = DesiredState.from_json(payload)
            document = validated.to_json()
            # The SQLite transaction is the only write and the only commit
            # point (U0a-2): a failure here lands nothing; a success is
            # immediately the authority.  No projection, no fallback, no
            # compensation -- one storage, no cross-storage window.
            self._sqlite_shadow.write_document(document)

    def set_service_connection(self, name: str, local_port: int) -> DesiredState:
        from .services import ServiceConnection

        connection = ServiceConnection.from_json({"name": name, "localPort": local_port})
        with self._lock:
            state = self.load()
            connections = {item.name: item for item in state.service_connections}
            connections[connection.name] = connection
            updated = replace(
                state,
                service_connections=tuple(connections[key] for key in sorted(connections)),
            )
            self.save(updated)
            return self.load()

    def remove_service_connection(self, name: str) -> DesiredState:
        if not isinstance(name, str) or not name:
            raise TypeError("service connection name must be a non-empty string")
        with self._lock:
            state = self.load()
            updated = replace(
                state,
                service_connections=tuple(
                    item for item in state.service_connections if item.name != name
                ),
            )
            self.save(updated)
            return self.load()

    def set_service_registry(
        self, cache: Mapping[str, object] | ServiceRegistry | None
    ) -> DesiredState:
        if cache is None:
            registry = None
        elif isinstance(cache, ServiceRegistry):
            registry = cache
        elif isinstance(cache, Mapping):
            registry = ServiceRegistry.from_json(cache)
        else:
            raise TypeError("service registry must be a mapping, ServiceRegistry, or None")
        with self._lock:
            updated = replace(self.load(), service_registry=registry)
            self.save(updated)
            return self.load()

    def require_runtime_schema(self) -> None:
        with self._lock:
            self.save(replace(self.load(), schema_version=2))

    def apply_harness_lifecycle(
        self,
        request: LifecycleMutationRequest,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> tuple[MutationProvenance, bool]:
        """Commit the lifecycle fence and receipt; ensure rows wait for U0b.

        Allen 2026-09-03: ``hyprial start`` must leave desired-state trace only
        AFTER the process is actually up, so an EnsureHarnessCommand here
        writes the resource fence and the (incomplete) receipt -- the
        idempotency and rollback machinery -- but NOT the harnesses row.
        The row lands in the same save as the receipt completion via
        :meth:`confirm_harness_lifecycle`, called from the start-success
        event.  RemoveHarnessCommand still deletes the row here (a removal
        whose process effect is in flight owns "gone" immediately, exactly
        as before).
        """

        from hyprial.daemon.impl.harnesses.runtime.ports  import EnsureHarnessCommand, RemoveHarnessCommand

        payload = request.payload
        if not isinstance(payload, (EnsureHarnessCommand, RemoveHarnessCommand)):
            raise TypeError(f"unsupported Harness lifecycle payload: {type(payload).__name__}")
        harness = payload.spec.harness if isinstance(payload, EnsureHarnessCommand) else payload.harness
        name = payload.spec.name if isinstance(payload, EnsureHarnessCommand) else payload.name
        key = f"harness:{harness}:{name}"
        with self._lock:
            state = self.load()
            replay = _stored_receipt(state, "harness", request, key)
            if replay is not None:
                return replay.provenance, True
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            current = harnesses.get((harness, name))
            resources = _resource_map(state)
            resource = _reconcile_resource(
                resources.get(("harness", key)),
                "harness",
                key,
                current is not None,
                {} if current is None else current.to_json(),
            )
            if isinstance(payload, EnsureHarnessCommand):
                desired = HarnessLaunchSpec.from_json(
                    payload.spec.to_payload(), "lifecycle.harness"
                )
                changed, created, resource = _apply_create_resource(
                    resource,
                    request.expected_resource_token,
                    desired.to_json(),
                )
                # U0b: no harnesses row here.  The row (and its
                # status="running") is written by confirm_harness_lifecycle
                # when the start actually succeeds; a start that never
                # succeeds leaves no trace, by design.
            else:
                changed, created, resource = _apply_delete_resource(
                    resource,
                    request.expected_resource_token,
                    {} if current is None else current.to_json(),
                )
                if changed:
                    harnesses.pop((harness, name), None)
            resources[("harness", key)] = resource
            provenance = MutationProvenance(created, changed, resource.resource_token)
            receipt = _new_stored_receipt(
                "harness",
                request,
                key,
                provenance,
                completed=False,
                generation=generation,
                version=version,
            )
            updated = replace(
                state,
                harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                lifecycle_resources=tuple(resources[item] for item in sorted(resources)),
                lifecycle_receipts=(*state.lifecycle_receipts, receipt),
            )
            self.save(updated)
            return provenance, False

    def confirm_harness_lifecycle(
        self,
        attempt_token: str,
        resource_token: str,
        spec: HarnessLaunchSpec,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> bool:
        """Complete an Ensure receipt AND write its desired-state row.

        This is the U0b commit point: called only from the start-success
        event, it lands the harnesses row (status "running" -- the process
        is up) and the completed receipt in ONE save.  If a row already
        exists (re-ensure of a running or previously failed harness) it is
        rewritten from ``spec`` with status "running": the spec that just
        started IS the user's latest intent, and an explicit start is the
        documented way out of ``failed``.
        """

        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if found is None or found.provenance.resource_token != resource_token:
                return False
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            harnesses[(spec.harness, spec.name)] = replace(
                spec, status="running"
            )
            if found.completed:
                updated_receipt = found
            else:
                updated_receipt = replace(
                    found,
                    completed=True,
                    generation=(
                        found.generation if generation is None else generation
                    ),
                    version=found.version if version is None else version,
                )
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                    lifecycle_receipts=tuple(
                        updated_receipt if item is found else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return True

    def mark_harness_failed(self, harness: str, name: str) -> bool:
        """Persist "ran before, did not come back" for an existing row.

        U0b/Allen 2026-09-03: a harness whose last known result was running
        and whose (re)start did not come up is recorded as ``failed`` --
        displayed, never auto-retried.  No-op (False) when no row exists: a
        start that never succeeded leaves no trace, so there is nothing to
        mark.
        """

        with self._lock:
            state = self.load()
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            current = harnesses.get((harness, name))
            if current is None:
                return False
            if current.status == "failed":
                return True
            harnesses[(harness, name)] = replace(current, status="failed")
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                )
            )
            return True

    def record_harness_lifecycle_failure(
        self, attempt_token: str, resource_token: str
    ) -> int:
        """Persist one failed internal process effect and return its count."""

        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if (
                found is None
                or found.provenance.resource_token != resource_token
                or found.completed
            ):
                raise ValueError("Harness lifecycle failure receipt mismatch")
            updated_receipt = replace(found, attempts=found.attempts + 1)
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        updated_receipt if item is found else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return updated_receipt.attempts

    def complete_harness_lifecycle(
        self,
        attempt_token: str,
        resource_token: str,
        *,
        generation: int | None = None,
        version: int | None = None,
    ) -> bool:
        with self._lock:
            state = self.load()
            found = _receipt_by_attempt(state, "harness", attempt_token)
            if found is None or found.provenance.resource_token != resource_token:
                return False
            if found.completed:
                return True
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        replace(
                            item,
                            completed=True,
                            generation=(
                                item.generation if generation is None else generation
                            ),
                            version=item.version if version is None else version,
                        )
                        if item is found
                        else item
                        for item in state.lifecycle_receipts
                    ),
                )
            )
            return True

    def incomplete_harness_lifecycle_resources(self) -> tuple[str, ...]:
        """Resource keys whose process effect still owns retry custody."""

        state = self.load()
        return tuple(
            sorted(
                receipt.resource_key.removeprefix("harness:")
                for receipt in state.lifecycle_receipts
                if receipt.domain == "harness" and not receipt.completed
            )
        )

    def incomplete_harness_lifecycle_receipts(
        self,
    ) -> tuple[StoredLifecycleReceipt, ...]:
        state = self.load()
        return tuple(
            receipt
            for receipt in state.lifecycle_receipts
            if receipt.domain == "harness" and not receipt.completed
        )

    def harness_lifecycle_receipt(
        self, attempt_token: str
    ) -> StoredLifecycleReceipt | None:
        return _receipt_by_attempt(self.load(), "harness", attempt_token)

    def rollback_harness_lifecycle(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        """Rollback only while the committed resource still owns its token."""

        with self._lock:
            state = self.load()
            receipt = _receipt_by_attempt(state, "harness", attempt_token)
            if receipt is None or receipt.provenance.resource_token != resource_token:
                return False
            resources = _resource_map(state)
            resource = resources.get(("harness", receipt.resource_key))
            if resource is None or resource.resource_token != resource_token:
                self.save(
                    replace(
                        state,
                        lifecycle_receipts=tuple(
                            item
                            for item in state.lifecycle_receipts
                            if item is not receipt
                        ),
                    )
                )
                return False
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            key = receipt.resource_key.removeprefix("harness:")
            harness, name = key.split(":", 1)
            if receipt.provenance.changed:
                if resource.active:
                    harnesses.pop((harness, name), None)
                    resource = replace(resource, active=False)
                else:
                    restored = HarnessLaunchSpec.from_json(
                        resource.payload, "lifecycle.rollback.harness"
                    )
                    harnesses[(harness, name)] = restored
                    resource = replace(resource, active=True)
            resources[("harness", receipt.resource_key)] = resource
            self.save(
                replace(
                    state,
                    harnesses=tuple(harnesses[item] for item in sorted(harnesses)),
                    lifecycle_resources=tuple(
                        resources[item] for item in sorted(resources)
                    ),
                    lifecycle_receipts=tuple(
                        item for item in state.lifecycle_receipts if item is not receipt
                    ),
                )
            )
            return True

    def fail_harness_removal(self, attempt_token: str, resource_token: str) -> bool:
        """Settle a failed stop without undoing the admitted removal intent.

        The receipt AND current resource token authorize this transaction. A
        stale failure may retire its own receipt, never a replacement's row.
        Explicitly delete the row here too: admission's earlier delete is not
        evidence that the row is still absent at terminal settlement.
        """
        with self._lock:
            state = self.load()
            receipt = _receipt_by_attempt(state, "harness", attempt_token)
            if (receipt is None or receipt.completed
                    or receipt.provenance.resource_token != resource_token):
                return False
            resources = _resource_map(state)
            resource = resources.get(("harness", receipt.resource_key))
            owns_resource = (
                receipt.provenance.changed
                and resource is not None
                and resource.resource_token == resource_token
            )
            harnesses = state.harnesses
            if owns_resource:
                harness, name = receipt.resource_key.removeprefix("harness:").split(":", 1)
                harnesses = tuple(item for item in harnesses
                                 if (item.harness, item.name) != (harness, name))
                resources[("harness", receipt.resource_key)] = replace(resource, active=False)
            self.save(replace(
                state,
                harnesses=harnesses,
                lifecycle_resources=tuple(resources[key] for key in sorted(resources)),
                lifecycle_receipts=tuple(item for item in state.lifecycle_receipts
                                         if item is not receipt),
            ))
            return owns_resource

    def lifecycle_effect_claims(self) -> tuple[DomainEffectClaim, ...]:
        """Every durable receipt as a backfill claim (U0c startup input)."""

        return tuple(
            DomainEffectClaim(
                receipt.operation_id,
                receipt.attempt_token,
                receipt.provenance.changed,
                receipt.provenance.created_by_operation,
                receipt.provenance.resource_token,
            )
            for receipt in self.load().lifecycle_receipts
        )

    def _journal_effect_completed(
        self, operation_id: str, attempt_token: str
    ) -> bool:
        """Whether the shared-db journal already settled this attempt.

        The desired-state tables and the lifecycle journal live in one
        SQLite file (U0a-2), so the receipt expiry can ask the journal
        directly which admissions the backfill left unsettled.
        """

        if not self.state_db.exists():
            return False
        try:
            with self.state_db.read() as db:
                row = db.execute(
                    "SELECT status FROM lifecycle_effects "
                    "WHERE operation_id = ? AND attempt_token = ?",
                    (operation_id, attempt_token),
                ).fetchone()
        except sqlite3.OperationalError:
            # A database that predates the journal (or a read-only home)
            # cannot account for any attempt -- treat as unsettled.
            return False
        return row is not None and str(row[0]) == "completed"

    def expire_interrupted_lifecycle_receipts(self) -> tuple[int, int]:
        """U0c: no lifecycle receipt crosses a daemon generation.

        Allen 2026-09-03: which step of a saga is done only matters in
        memory within one generation, so at startup every durable receipt
        belongs to a dead generation.  The restart compensates from the
        journal -- ``backfill_domain_attested_effects`` has already journaled
        what these receipts attest -- and re-runs from desired-state; it
        never replays an old attempt token.

        Receipts whose journal effect is STILL unsettled after the backfill
        are orphans of a saga the journal no longer knows (lost/legacy
        databases, receipts written outside a saga): the harness domain's
        own rollback undoes those admissions (fence flip + row restore --
        this is also what collects the U0b "ghost receipts" on homes whose
        journal predates them); every other receipt is deleted outright
        (retirement crash windows -- completed-unretired and
        retired-unconfirmed -- and session-domain orphans, whose undo
        without a plan would be guesswork).  Resource fences are otherwise
        NOT touched: they carry the tokens the journal's completed effects
        reference, which is exactly the compensation input the restart is
        about to use.  Returns ``(rolled_back, deleted)``.
        """

        rolled_back = 0
        with self._lock:
            for receipt in self.load().lifecycle_receipts:
                if receipt.completed:
                    # A completed effect's undo is compensation's job; a
                    # journal-unaccounted completed receipt is dead weight
                    # (orphan of a lost journal), deleted below.
                    continue
                if self._journal_effect_completed(
                    receipt.operation_id, receipt.attempt_token
                ):
                    continue
                if receipt.domain == "harness" and self.rollback_harness_lifecycle(
                    receipt.attempt_token, receipt.provenance.resource_token
                ):
                    rolled_back += 1
            state = self.load()
            deleted = len(state.lifecycle_receipts)
            if deleted:
                self.save(replace(state, lifecycle_receipts=()))
        return rolled_back, deleted

    def retire_lifecycle_receipt(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock:
            state = self.load()
            found = next(
                (
                    item
                    for item in state.lifecycle_receipts
                    if item.domain == domain and item.attempt_token == attempt_token
                ),
                None,
            )
            if (
                found is None
                or found.provenance.resource_token != resource_token
                or not found.completed
            ):
                return False
            if found.retired:
                return True
            receipts = tuple(
                replace(item, retired=True) if item is found else item
                for item in state.lifecycle_receipts
            )
            self.save(replace(state, lifecycle_receipts=receipts))
            return True

    def confirm_lifecycle_receipt_retired(
        self, domain: str, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock:
            state = self.load()
            found = next(
                (
                    item
                    for item in state.lifecycle_receipts
                    if item.domain == domain and item.attempt_token == attempt_token
                ),
                None,
            )
            if found is None:
                return False
            if not found.retired or found.provenance.resource_token != resource_token:
                raise ValueError("lifecycle receipt retirement mismatch")
            self.save(
                replace(
                    state,
                    lifecycle_receipts=tuple(
                        item for item in state.lifecycle_receipts if item is not found
                    ),
                )
            )
            return True

    def upsert_harness(self, spec: HarnessLaunchSpec) -> DesiredState:
        with self._lock:
            state = self.load()
            harnesses = {(item.harness, item.name): item for item in state.harnesses}
            # U0b: staging is an intent change, not a result change -- an
            # existing row keeps its last-known-result status (a failed
            # harness stays failed until an explicit start confirms it up);
            # a brand-new row starts at "running" so declared-then-restored
            # staging keeps its pre-U0b behavior.
            previous = harnesses.get((spec.harness, spec.name))
            harnesses[(spec.harness, spec.name)] = (
                spec if previous is None else replace(spec, status=previous.status)
            )
            updated = replace(
                state,
                harnesses=tuple(harnesses[key] for key in sorted(harnesses)),
            )
            updated = _with_external_resource(
                updated,
                "harness",
                f"harness:{spec.harness}:{spec.name}",
                True,
                spec.to_json(),
            )
            self.save(updated)
            return updated

    def remove_harness(self, harness: str, name: str) -> DesiredState:
        with self._lock:
            state = self.load()
            updated = replace(
                state,
                harnesses=tuple(
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) != (harness, name)
                ),
            )
            previous = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == (harness, name)
                ),
                None,
            )
            if previous is not None:
                updated = _with_external_resource(
                    updated,
                    "harness",
                    f"harness:{harness}:{name}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated
