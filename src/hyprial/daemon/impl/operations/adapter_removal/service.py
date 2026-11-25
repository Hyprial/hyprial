"""Compose existing removal reservations, file compensation and durable receipts.

Only stopped adapters are admitted. Confirmation covers configuration, binding,
credential digest and runtime version. Startup settles retained intents before
opening the request socket; it never replays a platform stop or deletes an app
at the external platform.

Decision note (I7a, contract i7-0 v2 section 2.4, owner item A-8): removal is
not refused for a bound adapter; the preview names the binding
(``boundAgent``).  The routing index no longer sees the outbound-only user
agent cell, so the name is the agent that receives on the adapter, else the
user agent when its cell document names the adapter.  That name only
informs, so a user agent cell that cannot be read names nobody instead of
failing the preview or the removal.  Removing an adapter the user agent
cell names goes through; the owner's copies then end ``TARGET_UNRESOLVED``
(``adapter-unconfigured``) until the adapter is added back.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from hyprial.kernel import ChannelConfiguration, DaemonRequestError, ipc_errors
from hyprial.daemon.impl.bootstrap.adapter_registration import (
    AdapterNotFoundError, _adapter_config_mutation, _json_bytes, _load_existing,
    _optional_bytes, _validate_name,
)
from .journal import AdapterRemovalJournal, RemovalIntent, RemovalPhase
from .transaction import RemovalTransaction


@dataclass(frozen=True)
class RemovalSnapshot:
    name: str
    digest: str
    before: bytes
    after: bytes
    registry: object
    entity_token: str | None
    credential_ref: str
    credential_digest: str | None
    remove_credential: bool
    generation: int
    version: int
    running: bool
    bound_agent: str | None
    routes: tuple[str, ...]


class AdapterRemovalService:
    def __init__(self, app):
        self.app = app
        self.home = Path(app.hyprial_home)
        self.journal = AdapterRemovalJournal(app.state_db)
        self.files = RemovalTransaction(self.home, self.journal)

    def _snapshot(self, name, registry):
        _validate_name(name)
        channels = _load_existing(self.home / "channels.json")
        gateway = next((item for item in channels.gateways if item.name == name), None)
        if gateway is None:
            raise AdapterNotFoundError("adapter is not configured")
        projection = self.app._lark_client.read_adapter(name)
        if projection is None:
            raise DaemonRequestError(ipc_errors.PREVIEW_STALE, "reload configuration before deletion")
        before = (self.home / "channels.json").read_bytes()
        remaining = tuple(item for item in channels.gateways if item.name != name)
        after = _json_bytes(ChannelConfiguration(gateways=remaining).to_json())
        credential_ref = gateway.credential_ref
        if any(part in credential_ref for part in ("/", "\\", "\0")):
            raise ValueError("invalid credential reference")
        credential = _optional_bytes(self.home / "secrets" / f"{credential_ref}.json")
        credential_digest = None if credential is None else hashlib.sha256(credential).hexdigest()
        remove_credential = credential_ref == f"lark-{name}" and not any(
            item.credential_ref == credential_ref for item in remaining
        )
        bound = self.app._adapter_routed_agents().get(name) or _user_agent_binding(self.app, name)
        entity = None
        generation = self.app._adapters.generation
        running = bool(projection.process_running or projection.online or projection.desired
                       or projection.lifecycle_correlation_id)
        identity = {
            "channels": hashlib.sha256(before).hexdigest(), "credential": credential_digest,
            "registration": None if registry.harness.spec is None else registry.harness.spec.to_json(),
            "generation": generation, "version": projection.version, "running": running,
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return RemovalSnapshot(name, digest, before, after, registry, entity, credential_ref,
                               credential_digest, remove_credential, generation, projection.version,
                               running, bound, tuple(route.name for route in gateway.routes))

    def observe(self, name):
        if self.journal.pending():
            raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "adapter removal requires recovery")
        with _adapter_config_mutation(self.home):
            with self.app._registry_management_handler().adapter_removal(name) as lease:
                return self._snapshot(name, lease.snapshot)

    def execute(self, name, expected_digest, operation_id, caller, confirm):
        runtime_held = False
        committed = False
        with _adapter_config_mutation(self.home):
            if self.journal.pending():
                raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "adapter removal requires recovery")
            try:
                with self.app._registry_management_handler().adapter_removal(name) as lease:
                    snapshot = self._snapshot(name, lease.snapshot)
                    if snapshot.digest != expected_digest:
                        raise DaemonRequestError(ipc_errors.PREVIEW_STALE, "adapter changed; preview again")
                    if snapshot.running:
                        raise DaemonRequestError(ipc_errors.OPERATION_IN_PROGRESS, "stop the adapter before deletion")
                    confirm(snapshot.digest)
                    runtime_held = True
                    self.app._lark_client.begin_removal(name, operation_id,
                        expected_generation=snapshot.generation, expected_version=snapshot.version)
                    intent = RemovalIntent(operation_id, name, caller, self.app.epoch, snapshot.digest,
                        snapshot.before, snapshot.after, snapshot.registry, snapshot.entity_token,
                        snapshot.credential_ref, snapshot.credential_digest, snapshot.remove_credential)
                    self.files.commit(intent, lease)
                    committed = True
                self.files.finish(operation_id, settle=lambda: self.app._lark_client.finish_removal(
                    name, operation_id, committed=True))
                runtime_held = False
                return {"ok": True, "name": name, "provider": "lark", "removed": True,
                        "unboundAgent": None,
                        "credentialRemoved": snapshot.remove_credential and snapshot.credential_digest is not None}
            except BaseException as error:
                record = self.journal.get(operation_id)
                settled = record is None or record.phase is RemovalPhase.COMPENSATED
                if runtime_held and settled and not committed:
                    try:
                        self.app._lark_client.finish_removal(name, operation_id, committed=False)
                    except Exception as cleanup_error:
                        error.add_note(f"runtime removal settlement failed: {type(cleanup_error).__name__}")
                raise

    def recover(self):
        recovered = False
        with _adapter_config_mutation(self.home):
            for record in self.journal.pending():
                intent = record.intent
                if record.phase is RemovalPhase.COMMITTED:
                    self.files.finish(intent.operation_id)
                else:
                    with self.app._registry_management_handler().adapter_removal(
                        intent.adapter, recovery_snapshot=intent.registry,
                    ) as lease:
                        self.files.compensate(intent.operation_id, lease)
                recovered = True
        if recovered:
            self.app._lark_client.reload()


def _user_agent_binding(app, name):
    from hyprial.daemon.impl.cells.adapter_routing import agent_adapter_bindings
    from hyprial.daemon.impl.user_agent import local_user_agent

    try:
        agent = local_user_agent(app.agents.list(), str(getattr(app, "owner", "") or ""))
        runtime = getattr(app, "_cell_runtime", None)
        if agent is None or runtime is None:
            return None
        bindings = agent_adapter_bindings(runtime.lookup(agent.cell_id))
    except Exception:
        return None
    return agent.uri if name in bindings else None


def recover_removals(app):
    journal = AdapterRemovalJournal(app.state_db)
    if journal.pending():
        AdapterRemovalService(app).recover()
