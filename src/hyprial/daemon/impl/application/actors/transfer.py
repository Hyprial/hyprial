"""Host transfer operations: plan/quiesce/land/receive/resume/complete and their resume invariants."""

from __future__ import annotations

from __future__ import annotations
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.identity import (
    AgentError,
    AgentHomeError as RegistryHomeError,
    HandoverNotice,
    PinConflictError,
)
from hyprial.daemon.impl.transfer.archive.bundle import BundleError
from hyprial.daemon.impl.transfer.landing.credentials import CredentialLandingError
from hyprial.daemon.impl.transfer.landing.owner import (
    LandingActorMismatch,
    LandingError,
    LandingNameTaken,
)
from hyprial.daemon.impl.transfer.archive.env_credentials import env_credentials_for_quiesce
from hyprial.daemon.impl.transfer.landing.registry import land_bundle_into_registry
from hyprial.daemon.impl.transfer.archive.signature import SignatureError
from hyprial.daemon.impl.transfer.archive.trust import TrustStoreError
from hyprial.daemon.impl.transfer.landing.signature_gate import trust_policy_for
from hyprial.daemon.impl.transfer.execution.sandbox_smoke import run_sandbox_smoke
from hyprial.daemon.impl.transfer.archive.session_files import (
    TRANSFERABLE_HARNESSES,
    SessionFileError,
    SessionFileNotFound,
    locate_session_file,
    pi_session_file,
)
from hyprial.kernel import ipc_errors, is_identity_id_segment
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
)
from hyprial.kernel import (
    canonical_user_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)
from dataclasses import replace
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    P2_CHAIN_MODE,
    ChainPlan,
    ChainPlanError,
    chain_bindings,
    chain_is_proven,
)
from hyprial.daemon.impl.transfer.admission import (
    TransferAdmissionError,
    resolve_transfer_target,
    transferable_spec,
)
from hyprial.daemon.impl.transfer.p2.source.effect import (
    SOURCE_QUIESCE_DEADLINE_SECONDS,
    QuiesceRequest,
)

#: How long this caller waits for the P2 source effect.  It outlasts the
#: effect's own budget so a timeout here is the effect's verdict, not ours.
P2_QUIESCE_WAIT_SECONDS = SOURCE_QUIESCE_DEADLINE_SECONDS + 30.0
P2_ADMISSION_WAIT_SECONDS = 10.0


_CREDENTIAL_LANDING_WIRE_CODES: dict[str, str] = {
    "credential_envelope_missing": ipc_errors.CREDENTIAL_ENVELOPE_MISSING,
    "credential_envelope_invalid": ipc_errors.CREDENTIAL_ENVELOPE_INVALID,
    "credential_host_key_refused": ipc_errors.CREDENTIAL_HOST_KEY_REFUSED,
}


class _TransferOpsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_transfer_land(self, params) -> Any:
        return self._transfer_land(params)

    def _ipc_transfer_plan(self, params) -> Any:
        return self._transfer_plan(params)

    def _ipc_transfer_quiesce(self, params) -> Any:
        return self._transfer_quiesce(params)

    def _ipc_transfer_precheck(self, params) -> Any:
        return self._transfer_precheck(params)

    def _ipc_transfer_receive(self, params) -> Any:
        return self._transfer_receive(params)

    def _ipc_transfer_resume(self, params) -> Any:
        return self._transfer_resume(params)

    def _ipc_transfer_complete(self, params) -> Any:
        return self._transfer_complete(params)

    def _transfer_desired_spec(self, params: JsonObject) -> HarnessLaunchSpec:
        raw_harness = params.get("provider")
        harness = (
            _required_string(raw_harness, "provider")
            if raw_harness is not None
            else None
        )
        name = _required_string(params.get("name"), "name")
        try:
            # Two tables, one decision: a managed harness is headless, an
            # interactive session is TUI, and a name in both is an ambiguity
            # (never a silent first-table pick).
            target = resolve_transfer_target(
                self.desired_state.load(), name=name, harness=harness
            )
            # The entrypoint fence runs before any headless-only assumption.
            spec = transferable_spec(target)
        except TransferAdmissionError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error
        if spec.execution_runtime is not None:
            raise DaemonRequestError(ipc_errors.TRANSFER_UNSUPPORTED_HARNESS, "smolvm cross-machine transfer awaits AT07")
        if spec.harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness} workers cannot be transferred: P0 supports "
                "pi, codex, and claude headless workers only (dsh has no "
                "resume by design; lark is an adapter, not a worker)",
            )
        # Deferred import: hyprial.harnesses re-enters hyprial.daemon at module load.
        from hyprial.daemon.impl.harnesses import is_streaming_spec

        if not is_streaming_spec(spec):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness}:{spec.name} is not a streaming headless "
                "worker; only streaming workers carry a resumable sessionRef",
            )
        return spec

    def _actor_pending_count(self, actor_uri: str, name: str) -> int | None:
        """Undrained inbox rows under both spellings; None when inbox is down."""

        if self._inbox is None:
            return None
        count = 0
        for key in {actor_uri, name}:
            count += len(self._inbox.pending_messages(key))
        return count

    def _transfer_payload(self, spec: HarnessLaunchSpec) -> JsonObject:
        agent = self.agents.get(spec.name)
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        return {
            "ok": True,
            "spec": spec.to_json(),
            "actor": actor_uri,
            "nodeId": self.node_id,
            "owner": self.owner,
            # Same strip as ``_agent_status_json``: ``Agent.to_json`` carries
            # the internal ``entityToken`` incarnation fence, which is
            # authority state, not a public IPC field.  The receiving side
            # never consumes it — ``transfer.receive`` mints a fresh hosted
            # incarnation — so removing it leaks nothing and breaks nothing.
            "agent": (
                {
                    key: value
                    for key, value in agent.to_json().items()
                    if key != "entityToken"
                }
                if agent is not None
                else None
            ),
            "unreadInbox": self._actor_pending_count(actor_uri, spec.name),
        }

    def _transfer_plan(self, params: JsonObject) -> JsonObject:
        """Read-only transfer inspection: the payload a quiesce would snapshot."""

        return self._transfer_payload(self._transfer_desired_spec(params))

    def _transfer_quiesce(self, params: JsonObject) -> JsonObject:
        """Stop the worker and snapshot everything the target needs.

        The session ref is force-synced BEFORE the snapshot so the payload
        carries the freshest ref, not the last reconcile tick's.  Removal
        mirrors ``down``: process stopped, desired-state entry dropped,
        liveness binding released.  The agent row and inbox rows stay --
        they leave only in ``transfer.complete``, after the target ACKs.
        """

        # Validate the target before asking the actor for a node-wide write-back.
        self._transfer_desired_spec(params)
        assert self._harnesses is not None
        self._harnesses.reconcile_session_refs()
        spec = self._transfer_desired_spec(params)
        operation_id = str(
            params.get("operationId") or f"transfer-quiesce:{uuid4().hex}"
        )
        chain = self._p2_chain_for_quiesce(params, spec, operation_id)
        if chain is None:
            self._run_lifecycle_operation(
                LifecycleOperation.deactivate(
                    operation_id,
                    self._lifecycle_spec(spec),
                )
            )
            payload = self._transfer_payload(spec)
            payload["stopped"] = True
        else:
            payload = self._quiesce_p2_chain(chain, spec, operation_id)
        self._log(
            "info",
            "transfer",
            "transfer.quiesced",
            actor=payload["actor"],
            sessionRef=spec.session_ref,
        )
        return payload

    def _p2_chain_for_quiesce(
        self, params: JsonObject, spec: HarnessLaunchSpec, operation_id: str
    ) -> ChainPlan | None:
        """The P2 chain this caller asked for, or ``None`` for a P0 transfer.

        The P2 facts are additive: without a ``planDigest`` this is exactly the
        P0 path it has always been, and no ledger is touched.  When they are
        present, both halves of the chain are derived here from one fact set, so
        they cannot drift (``lift.chain_bindings``).
        """

        digest = params.get("planDigest")
        if digest is None:
            return None
        if not chain_is_proven(spec.harness):
            # The P2 branch is a capability the source does not have yet: it
            # records a durable custody hold and stops the worker *through* it,
            # so it is closed until a real two-daemon run proves this harness.
            # The code and the mode string are the P0 refusal's own, so the
            # negative arm can be compared field by field.
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"agent-home P2 transfer is unsupported for {spec.harness}:"
                f"{spec.name}: the P2 chain has not been proven on two real "
                "daemons for this harness; refusing to hold and stop the "
                "source through it",
                {"harness": spec.harness, "name": spec.name, "mode": P2_CHAIN_MODE},
            )
        agent = self.agents.require(spec.name)
        try:
            return chain_bindings(
                operation_id=operation_id,
                owner=self.owner,
                source_node=self.node_id,
                source_actor=spec.name,
                source_entity_token=agent.entity_token,
                target_node=_required_string(params.get("targetNode"), "targetNode"),
                target_actor=_required_string(params.get("targetActor"), "targetActor"),
                plan_digest=_required_string(digest, "planDigest"),
            )
        except ChainPlanError as error:
            # A caller-supplied plan that cannot describe one chain is a bad
            # request, refused before either ledger is touched.
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error

    def _quiesce_p2_chain(
        self, chain: ChainPlan, spec: HarnessLaunchSpec, operation_id: str
    ) -> JsonObject:
        """Stop the worker *through* the P2 chain.

        Order matters and is the point of the chain: the admission is recorded
        first, so a crash before the worker stops still leaves a durable intent
        the recovery reader finds; then the effect derives the harness hold and
        stops the worker, so the source cannot be started again under the fence
        this chain owns.  ``unknown`` is reported as-is -- it is never rounded
        down to "stopped", and no rollback happens here.
        """

        if self._transfer_p2 is None or self._transfer_source is None:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                "the P2 source lane is unavailable on this daemon",
            )
        # All ledger writes go through the authority mailbox, never around it.
        try:
            self._transfer_p2.submit(
                Command(Operation.PREPARE, chain.operation_id, chain.source)
            ).result(P2_ADMISSION_WAIT_SECONDS)
        except AdmissionConflict as error:
            # ``reserved`` alone is not actionable: the refusal names the
            # preparation that holds this worker and the release that exists.
            raise self._reserved_preparation_refusal(
                chain, error.reason.value
            ) from error
        agent = self.agents.require(spec.name)
        outcome = self._quiesce_p2_source(
            QuiesceRequest(
                operation_id,
                spec.name,
                replace(
                    self._lifecycle_spec(spec), expected_entity_token=agent.entity_token
                ),
            )
        ).result(P2_QUIESCE_WAIT_SECONDS)
        payload = self._transfer_payload(spec)
        payload["stopped"] = outcome.state == "quiesced"
        payload["chain"] = {
            "operationId": chain.operation_id,
            "targetNode": chain.target.target_node,
            "targetActor": chain.target.target_actor,
            "planDigest": chain.target.plan_digest,
            "quiesce": {"state": outcome.state, "reason": outcome.reason},
            # The target's admission binds the SOURCE incarnation, and the
            # receiving API has always required the caller to carry it across.
            # Until now the only way to obtain it was the daemon's own SQLite
            # (the I4 run had to read it there), which made the documented flow
            # unrunnable end to end.  It is published here and ONLY here: on
            # the proven P2 branch of a quiesce the caller already asked for,
            # never by ``transfer.plan`` and never on the P0 path.  It is an
            # incarnation fence, not a credential: the same value already
            # travels inside the bundle as a digest, and the daemon's own
            # verdicts are still computed from its own registry row.
            "sourceEntityToken": agent.entity_token,
            # The home the CLI is about to export.  The CLI resolves its own
            # ``configured_hyprial_home()``, which names the same directory only
            # while both processes read the same environment; in a split-env
            # run (the state dir pointed at the daemon, HYPRIAL_HOME unset) the
            # guess names a *different* home and the bundle would carry the
            # right identity stamp over the wrong tree.  The daemon answers
            # with the path it actually serves (B6).
            "sourceHome": str(self.hyprial_home / "agents" / spec.name),
            # The home whose Ed25519 key stamps the exported bundle.  It is the
            # DAEMON's own home, never the CLI's resolved one: the key is the
            # machine's identity, the bundle is signed after quiesce by whoever
            # exports it, and a split-env CLI would otherwise sign with a home
            # this daemon never served (B6's lesson, applied to the signature).
            # The key itself is minted on first export and never leaves the
            # machine -- it is nowhere inside the exported payload.
            "signingHome": str(self.hyprial_home),
            # Env-key credentials, resolved by this daemon's own gate (see
            # archive/env_credentials.py); the CLI seals them into the envelope.
            "envCredentials": env_credentials_for_quiesce(
                registry=self._agent_registry,
                hyprial_home=self.hyprial_home,
                spec=spec,
            ),
        }
        return payload

    def _transfer_land(self, params: JsonObject) -> JsonObject:
        """Land a received AT06 bundle on this node (AT07, the identity half).

        The receive side of a bundle transfer: the bytes arrive as a bundle
        directory, and this call turns them into a local agent -- a fresh
        incarnation minted through the registry, the payload moved into the
        home that incarnation owns, and the agent-private grants recorded in
        the manifest materialised.  A name already taken is refused before a
        byte moves, and any failure after that is undone in reverse.

        What this deliberately does not do (card 356, still open): container
        materialisation, strict resume, and the two-machine run.  There is no
        ``transfer.land`` caller in the CLI yet either -- the orchestrator that
        ships a bundle and then lands it is the next slice.
        """

        bundle_dir = _required_string(params.get("bundleDir"), "bundleDir")
        expected_actor = params.get("actor")
        if expected_actor is not None and (
            not isinstance(expected_actor, str) or not expected_actor
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string"
            )
        dry_run = params.get("dryRun", False)
        if not isinstance(dry_run, bool):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "dryRun must be a boolean"
            )
        staging = self.state_dir / "transfer-landing" / uuid4().hex
        try:
            result = land_bundle_into_registry(
                bundle_dir,
                registry=self.agents,
                home_root=self.hyprial_home / "agents",
                machine_home=self.hyprial_home,
                staging=staging,
                granted_by=canonical_user_uri(self.owner),
                expected_actor=expected_actor,
                dry_run=dry_run,
                signature=trust_policy_for(self.hyprial_home),
            )
        except LandingNameTaken as error:
            raise DaemonRequestError(ipc_errors.TRANSFER_CONFLICT, str(error)) from error
        except LandingActorMismatch as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        except CredentialLandingError as error:
            # The credential gate is a landing refusal with its own wire codes:
            # an operator must be able to tell "this bundle declares a policy
            # this target will not take" from "the landing broke half-way".
            raise DaemonRequestError(
                _CREDENTIAL_LANDING_WIRE_CODES.get(
                    error.code, ipc_errors.TRANSFER_LANDING_FAILED
                ),
                str(error),
            ) from error
        except (SignatureError, TrustStoreError) as error:
            # AT06 slice 2b: attribution is a precondition, not a warning.  The
            # refusal reuses the landing-failed wire code (no new wire surface);
            # ``error.code`` in the text is what names the reason.
            raise DaemonRequestError(
                ipc_errors.TRANSFER_LANDING_FAILED,
                f"the bundle was not attributed "
                f"({getattr(error, 'code', '')}): {error}",
            ) from error
        except LandingError as error:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_LANDING_FAILED, str(error)
            ) from error
        except BundleError as error:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_LANDING_FAILED,
                f"the bundle was refused: {error}",
            ) from error
        payload: JsonObject = {"ok": True}
        payload.update(result.as_dict())
        self._log(
            "info",
            "transfer",
            "transfer.landed" if not dry_run else "transfer.land.dry_run",
            actor=result.actor,
            destination=str(result.destination),
            files=result.files,
            grants=len(result.grants),
        )
        return payload

    def _transfer_precheck(self, params: JsonObject) -> JsonObject:
        """Target-side admission: identity facts plus every name conflict."""

        harness = _required_string(params.get("provider"), "provider")
        name = _required_string(params.get("name"), "name")
        if harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{harness} workers cannot be transferred: P0 supports pi, "
                "codex, and claude headless workers only",
            )
        conflicts: list[str] = []
        # ⚠️ PREMISE, load-bearing and easy to break silently: all three
        # sources below are LOCAL TO THIS NODE.  The comparisons match on the
        # actor name alone — neither ``<owner>`` nor ``<machine>`` — which is
        # correct only because "does this node already speak for this name?"
        # is exactly the question, and nothing here can see another node.
        #
        # ⛔ It fails like this: give any of these three a cross-machine
        # source (a peer-aware session view, a registry that federates, a
        # desired-state that carries other nodes' harnesses) and a name in use
        # on a *peer* starts reporting as a local conflict — blocking a
        # transfer that is perfectly legal here.  Nothing would go red; the
        # name match would simply start matching more.
        #
        # This is the same shape as the defect ``_resolve_agent_alias`` just
        # had: a comparison that ignores ``<machine>`` and is safe only while
        # something unwritten keeps peers out of its inputs.  There the
        # protection was "owner happens to differ per host" and owner
        # unification removed it.  Here the protection is "these sources
        # happen to be local" — so it is written down.
        state = self.desired_state.load()
        for spec in state.harnesses:
            if spec.name == name:
                conflicts.append(f"managed harness {spec.harness}:{spec.name}")
        if self.agents.exists(name):
            conflicts.append(f"registered agent {name!r}")
        for session in self._agent_session_domains.session.read_sessions():
            parsed = parse_agent_uri(session.actor)
            if parsed is not None and parsed[2] == name:
                conflicts.append(f"interactive session {session.actor}")
        return {
            "ok": True,
            "nodeId": self.node_id,
            "owner": self.owner,
            "conflicts": conflicts,
        }

    def _transfer_receive(self, params: JsonObject) -> JsonObject:
        """Adopt a transferred worker: identity, spec, pins, strict resume.

        Failure at ANY point undoes every trace (desired-state entry, agent
        row, pins, persona route, liveness binding) so a rejected receive
        leaves the target exactly as it was -- the source still holds the
        worker and rolls back cleanly.
        """

        if params.get("planDigest") is not None:
            return self._transfer_receive_p2(params)
        spec = HarnessLaunchSpec.from_json(params.get("spec"), "spec")
        if spec.pinned_owner is not None and is_identity_id_segment(
            spec.pinned_owner.lower()
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "transfer pinned owner must not have an exact identity id shape",
            )
        if spec.execution_runtime is not None:
            raise DaemonRequestError(ipc_errors.TRANSFER_UNSUPPORTED_HARNESS, "smolvm transfer is not implemented")
        if spec.harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness} workers cannot be transferred: P0 supports "
                "pi, codex, and claude headless workers only",
            )
        from hyprial.daemon.impl.harnesses import is_streaming_spec

        if not is_streaming_spec(spec):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness}:{spec.name} is not a streaming headless worker",
            )
        state = self.desired_state.load()
        if any(existing.name == spec.name for existing in state.harnesses):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"a managed harness named {spec.name!r} already exists on "
                f"{self.node_id}",
            )
        if self.agents.exists(spec.name):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"the name {spec.name!r} is already taken on {self.node_id}",
            )
        raw_pins = params.get("pins", [])
        if not isinstance(raw_pins, list) or any(
            not isinstance(item, str) or not item for item in raw_pins
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "pins must be an array of adapter names"
            )
        timeout_raw = params.get("strictTimeoutSeconds", 90.0)
        if isinstance(timeout_raw, bool) or not isinstance(
            timeout_raw, (int, float)
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "strictTimeoutSeconds must be a number"
            )
        # Mapping-time sandbox smoke (design §4 / card 356 item 0): the
        # payload is on the target and the session is not resumed yet, so a
        # sandbox that cannot be shown to enforce must refuse HERE, before
        # any registry row, pin or process exists to undo.
        smoke = run_sandbox_smoke(spec.harness, containerized=spec.containerized)
        if smoke.status == "FAIL":
            raise DaemonRequestError(
                ipc_errors.SANDBOX_SMOKE_FAILED,
                f"{spec.harness}:{spec.name} refused the sandbox smoke on "
                f"{self.node_id}: {smoke.detail}",
                smoke.as_dict(),
            )
        if smoke.status == "NOT_RUN":
            self._log(
                "warn",
                "transfer",
                "transfer.sandbox_smoke.not_run",
                harness=spec.harness,
                detail=smoke.detail,
            )
        if spec.containerized:
            # Container mode (docs/design-transfer-container.md): load the
            # staged image tar when present (docker save chain, D-C),
            # create the per-worker credential volume, and shred the
            # staging dir -- the credential residual window on disk is
            # this call itself (D-A).
            from hyprial.daemon.impl.transfer.execution import container as xfer_container

            credentials = params.get("credentials", True) is not False
            staging = xfer_container.staging_dir(self.state_dir, spec.name)
            if credentials and not staging.is_dir():
                raise DaemonRequestError(
                    ipc_errors.TRANSFER_CREDENTIALS,
                    f"no credential staging at {staging}; the orchestrator "
                    "ships the bundle before receive",
                )
            try:
                xfer_container.prepare_worker(
                    xfer_container.DockerRunner(),
                    image=spec.container_image or xfer_container.default_image(),
                    name=spec.name,
                    staging=staging,
                    state_dir=self.state_dir,
                    harness=spec.harness,
                    credentials=credentials,
                )
            except xfer_container.ContainerError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        operation_id = str(
            params.get("operationId") or f"transfer-receive:{uuid4().hex}"
        )
        # Decision A: receive is the only implemented authority for hosting a
        # pinned owner. Insert before lifecycle bind/release can resolve its
        # URI. A plain create/start must never manufacture this authority.
        if spec.pinned_owner is not None:
            self.agents.create_transfer_hosted(
                spec.name, pinned_owner=spec.pinned_owner, cwd=spec.cwd,
                harness_args={spec.harness: spec.args},
                preferred_harness=spec.harness,
            )
            # No default routine here, deliberately.  A received identity is
            # staged (source fenced, target not yet complete), so nothing may
            # schedule it before cutover; the transfer line restores the
            # source's routines, or binds the default, when the move
            # completes (AT06; codex-sw, 2026-09-28).
        try:
            self._run_lifecycle_operation(
                LifecycleOperation.create(
                    operation_id,
                    self._lifecycle_spec(spec),
                ),
                timeout=max(70.0, float(timeout_raw) + 5.0),
            )
            for adapter in raw_pins:
                # A re-pin is a MOVE by registry semantics: without this gate
                # a transfer would silently steal the adapter from whichever
                # agent holds it on the target.  Refuse and name the holder.
                holder = self.agents.pins().get(adapter)
                if holder is not None and holder != actor_uri:
                    raise DaemonRequestError(
                        ipc_errors.TRANSFER_PIN_CONFLICT,
                        f"adapter {adapter!r} is already pinned to {holder} "
                        f"on {self.node_id}; unpin it there first",
                    )
                try:
                    self.agents.pin(adapter, spec.name)
                except PinConflictError as error:
                    raise DaemonRequestError(
                        ipc_errors.TRANSFER_PIN_CONFLICT, str(error)
                    ) from error
            assert self._harnesses is not None
            if spec.session_ref is not None:
                ready = self._harnesses.wait_ready(
                    spec.harness, spec.name, float(timeout_raw)
                )
                if not ready:
                    raise DaemonRequestError(
                        ipc_errors.STRICT_RESUME_FAILED,
                        f"{spec.harness}:{spec.name} did not become ready "
                        f"within {float(timeout_raw)}s on {self.node_id}; "
                        "the transferred session could not be resumed",
                    )
                resumed = self._harnesses.session_refs().get(
                    (spec.harness, spec.name)
                )
                if resumed != spec.session_ref:
                    raise DaemonRequestError(
                        ipc_errors.STRICT_RESUME_FAILED,
                        f"resume of session {spec.session_ref!r} did not "
                        f"hold on {self.node_id}: the worker established "
                        f"{resumed!r} instead (a cold start would silently "
                        "lose the transferred conversation)",
                    )
        except BaseException:
            self._transfer_undo_receive(spec, actor_uri)
            raise
        self._log(
            "info",
            "transfer",
            "transfer.received",
            actor=actor_uri,
            sessionRef=spec.session_ref,
        )
        return {
            "ok": True,
            "actor": actor_uri,
            "nodeId": self.node_id,
            "sessionRef": spec.session_ref,
            "sandboxSmoke": smoke.as_dict(),
            "operationId": operation_id,
        }

    def _require_resumable_session(self, spec: HarnessLaunchSpec, session_ref: str) -> None:
        """Refuse a resume whose transcript the harness would not find.

        Checked BEFORE anything starts, in the same HOME the daemon hands its
        children: pi given an unknown ``--session-id`` warns and begins a
        fresh session under that very id, so the post-start id comparison in
        :meth:`_verify_started_resume` passes on a cold start.  For pi this
        file check is the gate that holds.
        """

        if spec.harness not in TRANSFERABLE_HARNESSES or not spec.headless:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"resuming a session is supported for headless "
                f"{', '.join(sorted(TRANSFERABLE_HARNESSES))} only, not "
                f"{spec.harness}{'' if spec.headless else ' (interactive)'}",
            )
        if spec.cwd is not None:
            cwd = spec.cwd
        else:
            try:
                cwd = self._agent_session_domains.agent.prepare_workspace(spec.name)
            except RegistryHomeError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve default workspace for {spec.name}: {error}",
                ) from error
        runtime_context = None
        agent = self.agents.get(spec.name)
        if agent is not None and agent.config is not None:
            from hyprial.identity import AgentConfigError
            from hyprial.identity import AgentHomeError
            from hyprial.identity import (
                DEFAULT_AGENT_TOOL_PROFILE,
                AgentRuntimeError,
            )

            try:
                runtime_context = self._agent_session_domains.agent.prepare_runtime_context(
                    agent.actor,
                    spec.harness,
                    cwd,
                    DEFAULT_AGENT_TOOL_PROFILE,
                    containerized=spec.containerized,
                    legacy_reporter=self._report_legacy_agent_home,
                )
            except (
                AgentError,
                AgentConfigError,
                AgentHomeError,
                AgentRuntimeError,
            ) as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve agent-home P2 session root for "
                    f"{spec.harness}:{spec.name}: {error}",
                ) from error
            if runtime_context is None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"agent-home P2 session root is unavailable for "
                    f"{spec.harness}:{spec.name}",
                )
        try:
            agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
            if runtime_context is None and spec.harness == "pi" and agent_dir:
                located = pi_session_file(Path(agent_dir).expanduser(), cwd, session_ref)
            else:
                located = locate_session_file(
                    spec.harness,
                    cwd,
                    session_ref,
                    home=Path.home(),
                    runtime_context=runtime_context,
                )
            if not located.is_file():
                raise SessionFileNotFound(f"not a regular session file: {located}")
        except SessionFileError as error:
            raise DaemonRequestError(
                ipc_errors.RESUME_SESSION_NOT_FOUND,
                f"cannot resume {spec.harness} session {session_ref!r} for "
                f"{spec.name}: {error}; nothing was started",
                {
                    "harness": spec.harness,
                    "name": spec.name,
                    "sessionRef": session_ref,
                    "cwd": cwd,
                    "reason": (
                        "not-found"
                        if isinstance(error, SessionFileNotFound)
                        else "unusable"
                    ),
                },
            ) from error

    def _verify_started_resume(
        self, spec: HarnessLaunchSpec, session_ref: str, *, timeout: float
    ) -> str:
        """The started worker must be ON the requested session, or not run.

        Same check as transfer.receive's strict resume.  On failure the worker
        is deactivated: an error with a fresh-session worker still running
        behind it under the same name is the one outcome worse than either.
        """

        assert self._harnesses is not None
        ready = self._harnesses.wait_ready(spec.harness, spec.name, timeout)
        resumed = (
            self._harnesses.session_refs().get((spec.harness, spec.name))
            if ready
            else None
        )
        if ready and resumed == session_ref:
            return resumed
        self._run_lifecycle_operation(
            LifecycleOperation.deactivate(
                f"lifecycle-start-resume-undo:{uuid4().hex}",
                self._lifecycle_spec(spec),
            )
        )
        detail = (
            f"did not become ready within {timeout}s"
            if not ready
            else f"established session {resumed!r} instead"
        )
        raise DaemonRequestError(
            ipc_errors.STRICT_RESUME_FAILED,
            f"resume of {spec.harness} session {session_ref!r} for {spec.name} "
            f"did not hold: the worker {detail}; it was stopped rather than "
            "left running on a fresh session",
            {
                "harness": spec.harness,
                "name": spec.name,
                "sessionRef": session_ref,
                "established": resumed,
            },
        )

    def _transfer_undo_receive(self, spec: HarnessLaunchSpec, actor_uri: str) -> None:
        """Compensate a failed receive through the same durable saga owner.

        REMOVE (not DEACTIVATE) erases the pre-inserted hosted row and its
        cascading pins even when CREATE reused that row. This closes the
        agent-row residue in b39d7d26; down deliberately keeps the entity.
        """

        del actor_uri
        self._run_lifecycle_operation(
            LifecycleOperation.remove(
                f"transfer-undo:{uuid4().hex}",
                self._lifecycle_spec(spec),
            )
        )
        if spec.containerized:
            self._retire_container_artifacts(spec.harness, spec.name)

    def _retire_container_artifacts(
        self, harness: str, name: str
    ) -> list[str]:
        """Decision D-A: retirement removes containers, the volume, the home.

        Best effort and loud: leftovers come back in the result (and the
        log) instead of masking the operation that triggered the cleanup.
        """

        from hyprial.daemon.impl.transfer.execution import container as xfer_container

        try:
            problems = xfer_container.prune_worker(
                xfer_container.DockerRunner(),
                harness=harness,
                name=name,
                state_dir=self.state_dir,
            )
        except Exception as error:  # noqa: BLE001 - report, never mask
            problems = [str(error)]
        if problems:
            self._log(
                "warn",
                "transfer",
                "container.retire.leftovers",
                harness=harness,
                # Not ``name=``: the logger owns that field and refuses it from
                # callers, so this path crashed instead of reporting leftovers.
                agent=name,
                leftovers=problems,
            )
        return problems

    def _transfer_resume(self, params: JsonObject) -> JsonObject:
        """Source-side rollback: put the quiesced worker back exactly as it was.

        Deliberately NOT strict: this path restores the operator's pre-transfer
        state on the machine where the session files never left, so a dead ref
        degrades to #190's ordinary cold-start fallback rather than compounding
        the original failure.
        """

        spec = HarnessLaunchSpec.from_json(params.get("spec"), "spec")
        if spec.execution_runtime is not None:
            raise DaemonRequestError(ipc_errors.TRANSFER_UNSUPPORTED_HARNESS, "smolvm transfer is not implemented")
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        prior_agent = self.agents.get(actor_uri)
        operation_id = str(
            params.get("operationId") or f"transfer-resume:{uuid4().hex}"
        )
        result = self._run_lifecycle_operation(
            LifecycleOperation.create(
                operation_id,
                self._lifecycle_spec(spec),
            )
        )
        handover = (
            HandoverNotice(
                actor=prior_agent.actor,
                previous_harness=prior_agent.last_harness,
                previous_session_id=prior_agent.last_session_id,
                next_harness=spec.harness,
            )
            if prior_agent is not None
            and prior_agent.last_harness is not None
            and prior_agent.last_harness != spec.harness
            else None
        )
        assert self._harnesses is not None
        resumed = self._harnesses.session_refs().get((spec.harness, spec.name))
        self._log(
            "info",
            "transfer",
            "transfer.rolled_back",
            actor=actor_uri,
            sessionRef=resumed,
        )
        return {
            "ok": True,
            "actor": actor_uri,
            "changed": bool(result.completed_effects),
            "operationId": operation_id,
            "sessionRef": resumed,
            **(
                {"harnessHandover": handover.to_json()}
                if handover is not None
                else {}
            ),
        }

    def _transfer_complete(self, params: JsonObject) -> JsonObject:
        """Source-side cleanup after the target ACKs: retire the old identity.

        Gentler than ``agent.destroy`` on purpose: pending inbox rows are NOT
        drained -- they remain readable under the old URI (P0's keep-inbox
        semantics; forwarding is P1).  The agent row's deletion cascades the
        pins, and the persona route leaves so the old URI stops promising
        delivery it can no longer drain into a live worker.
        """

        if params.get("chain") is not None:
            # Additive P2 facts: the caller brings the target's report, and the
            # decision (in the P2 package) says whether this chain is complete.
            # Without them this is the P0 path it has always been.
            return self._transfer_complete_p2(params)
        name = self.agents.normalize_actor(
            _required_string(params.get("name"), "name")
        )
        agent = self.agents.get(name)
        if agent is None:
            return {"ok": True, "removedAgent": False}
        actor = agent.uri
        unpinned = sorted(agent.pinned_adapters)
        requested_harness = params.get("provider")
        harness = (
            requested_harness
            if isinstance(requested_harness, str) and requested_harness
            else agent.last_harness or "pi"
        )
        operation_id = str(
            params.get("operationId") or f"transfer-complete:{uuid4().hex}"
        )
        self._run_lifecycle_operation(
            LifecycleOperation.remove(
                operation_id,
                self._lifecycle_spec(
                    HarnessLaunchSpec(harness, name, True)
                ),
            )
        )
        removed = self.agents.get(name) is None
        # D-A: the worker moved away -- its container artifacts (labelled
        # containers, credential volume, worker home) retire with it.
        container_leftovers: list[str] = []
        if isinstance(requested_harness, str) and requested_harness:
            container_leftovers = self._retire_container_artifacts(
                requested_harness, name
            )
        self._log(
            "info",
            "transfer",
            "transfer.completed",
            actor=actor,
            unpinnedAdapters=unpinned,
        )
        return {
            "ok": True,
            "removedAgent": removed,
            "operationId": operation_id,
            "actor": actor,
            "unpinnedAdapters": unpinned,
            "unreadInbox": self._actor_pending_count(actor, name),
            **(
                {"containerLeftovers": container_leftovers}
                if container_leftovers
                else {}
            ),
        }
