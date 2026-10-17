"""AT07 I3-c: the target-side entry -- admit one half, then land and activate.

``target.land_and_activate`` is the driver; what was missing is the entry that
*admits* the operation's target half and hands it the bundle.  This module is
that entry, and it is deliberately the only place the P2 target store is built:
the ledger it writes is the daemon's own, the same tables the authority mailbox
writes, so there is still one answer to "did this step run".

Same fence as the source side, and the same words: the branch is closed until a
real two-daemon run proves the harness, and the refusal carries the P0 refusal's
own code and mode string so the two arms of one fence compare field by field.
The caller brings the *same* fact set the source used, and the target binding is
derived through the one composer -- a second composition spelled differently
would look healthy on both ledgers and hold forever at ``plan-mismatch``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from hyprial.identity import DEFAULT_AGENT_TOOL_PROFILE, PinConflictError
from hyprial.kernel import DaemonRequestError, canonical_user_uri, ipc_errors

from hyprial.daemon.impl.transfer.archive.bundle import BundleError
from hyprial.daemon.impl.transfer.archive.signature import SignatureError
from hyprial.daemon.impl.transfer.archive.trust import TrustStoreError
from hyprial.daemon.impl.transfer.landing.signature_gate import SignaturePolicy
from hyprial.daemon.impl.transfer.archive.session_files import TRANSFERABLE_HARNESSES
from hyprial.daemon.impl.transfer.execution.sandbox_smoke import run_sandbox_smoke
from hyprial.daemon.impl.transfer.landing.credentials import CredentialLandingError
from hyprial.daemon.impl.transfer.landing.owner import LandingError, LandingNameTaken
from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
    ENTRYPOINT_HEADLESS,
    P2_CHAIN_MODE,
    ChainPlan,
    ChainPlanError,
    TargetHalf,
    admit_target,
    chain_bindings,
    chain_is_proven,
)
from hyprial.daemon.impl.transfer.p2.orchestration.target import (
    RuntimeContextActivation,
    TargetLanding,
    TargetLandingError,
    land_and_activate,
)
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

_CREDENTIAL_CODES: dict[str, str] = {
    "credential_envelope_missing": ipc_errors.CREDENTIAL_ENVELOPE_MISSING,
    "credential_envelope_invalid": ipc_errors.CREDENTIAL_ENVELOPE_INVALID,
    "credential_host_key_refused": ipc_errors.CREDENTIAL_HOST_KEY_REFUSED,
}


@dataclass(frozen=True, slots=True)
class ReceivedTarget:
    """One target landing and the chain it belongs to.

    ``half`` is what the caller carries back: this daemon's own report of the
    target half, shipped so the source can weigh its completion against it.
    It is a report, not proof -- ``lift.TargetHalf`` says why, and what would
    make it proof.
    """

    plan: ChainPlan
    landing: TargetLanding
    half: TargetHalf


def _string(params: Mapping[str, object], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{key} must be a non-empty string"
        )
    return value


def _adapter_names(value: object) -> list[str]:
    """The adapters the source row holds, spelled the one way P0 spells it."""

    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "pins must be an array of adapter names"
        )
    return list(value)


def _refuse_pin_conflicts(
    registry: object, actor: str, pins: Sequence[str], node_id: str
) -> None:
    """P0's pin gate, read-only and before any write.

    A re-pin is a MOVE by registry semantics, so the gate names the holder and
    refuses; it is asked here so a conflict stops the landing while this target
    still has nothing to undo.  A race between this read and the write is
    caught by ``pin`` itself in ``_apply_pins``.
    """

    if not pins:
        return
    target_uri = registry.uri_for(actor)
    held = registry.pins()
    for adapter in pins:
        holder = held.get(adapter)
        if holder is not None and holder != target_uri:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_PIN_CONFLICT,
                f"adapter {adapter!r} is already pinned to {holder} on "
                f"{node_id}; unpin it there first",
            )


def _apply_pins(registry: object, actor: str, pins: Sequence[str]) -> None:
    """Re-pin the source's adapters onto the landed actor, one at a time.

    The rows exist by now, so a pin that still conflicts is a race, not a
    pre-flight miss; it is reported with the same code the gate above uses.
    """

    for adapter in pins:
        try:
            registry.pin(adapter, actor)
        except PinConflictError as error:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_PIN_CONFLICT, str(error)
            ) from error


def _refuse_unproven(harness: str, name: str, entrypoint: str = ENTRYPOINT_HEADLESS) -> None:
    """The reachability fence: no unproven (harness, mode, entrypoint) lands."""

    if chain_is_proven(harness, P2_CHAIN_MODE, entrypoint):
        return
    raise DaemonRequestError(
        ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
        f"agent-home P2 transfer is unsupported for {harness}:{name} "
        f"entrypoint={entrypoint}: the P2 chain has not been proven on two "
        "real daemons for this entrypoint; refusing to land it on an "
        "unproven target",
        {"harness": harness, "name": name, "entrypoint": entrypoint, "mode": P2_CHAIN_MODE},
    )


def receive_into_target(
    params: Mapping[str, object],
    *,
    spec: object,
    owner: str,
    node_id: str,
    state_db: object,
    registry: object,
    home_root: Path,
    machine_home: Path | None = None,
    staging_dir: Path,
    environment: Mapping[str, str],
    signature: SignaturePolicy,
    entrypoint: str = ENTRYPOINT_HEADLESS,
) -> ReceivedTarget:
    """Admit this daemon's half of the chain, then land and activate the bundle.

    Raises ``DaemonRequestError`` with the wire code the P0 receive/land paths
    already use, so an operator cannot tell an unproven target from any other
    refusal and a bad request from a broken landing.
    """

    harness = str(getattr(spec, "harness"))
    name = str(getattr(spec, "name"))
    # The operator's resolved destination cwd travels in the same spec the P0
    # receive reads; the landing mints the row with it and the activation binds
    # the runtime context to it, instead of falling back to a row that has
    # none (B7).
    cwd = getattr(spec, "cwd", None)
    pins = _adapter_names(params.get("pins", []))
    _refuse_unproven(harness, name, entrypoint)
    if harness not in TRANSFERABLE_HARNESSES:
        raise DaemonRequestError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"{harness} workers cannot be transferred: P0 supports pi, codex, "
            "and claude headless workers only",
        )
    operation_id = _string(params, "operationId")
    try:
        plan = chain_bindings(
            operation_id=operation_id,
            owner=owner,
            source_node=_string(params, "sourceNode"),
            source_actor=_string(params, "sourceActor"),
            source_entity_token=_string(params, "sourceEntityToken"),
            # The target node is where this code is running; the caller cannot
            # spell it, so it cannot spell it two ways.
            target_node=node_id,
            target_actor=_string(params, "targetActor"),
            plan_digest=_string(params, "planDigest"),
        )
    except ChainPlanError as error:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error

    # The pin gate is a bad-request refusal like a bad plan: it is asked before
    # this daemon records anything of its own, so a conflict leaves the target
    # exactly as it was (B1, P0's ordering).
    _refuse_pin_conflicts(registry, name, pins, node_id)
    store = AdmissionStore(state_db, node=node_id, owner=owner)
    admit_target(store, plan)
    try:
        landing = land_and_activate(
            operation_id,
            store=store,
            bundle_dir=_string(params, "bundleDir"),
            registry=registry,
            home_root=home_root,
            machine_home=machine_home,
            staging=staging_dir / uuid4().hex,
            granted_by=canonical_user_uri(owner),
            cwd=cwd,
            harness=harness,
            activation=RuntimeContextActivation(
                registry,
                harnesses=(harness,),
                tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
                base_environment=environment,
                cwd=cwd,
                containerized=bool(getattr(spec, "containerized", False)),
            ),
            sandbox_smoke=lambda: run_sandbox_smoke(
                harness, containerized=bool(getattr(spec, "containerized", False))
            ),
            expected_actor=name,
            signature=signature,
            entrypoint=entrypoint,
        )
    except TargetLandingError as error:
        code = (
            ipc_errors.SANDBOX_SMOKE_FAILED
            if error.code == "sandbox-smoke-failed"
            else ipc_errors.TRANSFER_LANDING_FAILED
        )
        raise DaemonRequestError(code, str(error)) from error
    except LandingNameTaken as error:
        raise DaemonRequestError(ipc_errors.TRANSFER_CONFLICT, str(error)) from error
    except CredentialLandingError as error:
        raise DaemonRequestError(
            _CREDENTIAL_CODES.get(error.code, ipc_errors.TRANSFER_LANDING_FAILED),
            str(error),
        ) from error
    except (SignatureError, TrustStoreError) as error:
        # AT06 slice 2b: an unattributable bundle is refused before anything of
        # this daemon's own is recorded.  Same wire code as any landing refusal;
        # ``error.code`` in the text is what names the reason.
        raise DaemonRequestError(
            ipc_errors.TRANSFER_LANDING_FAILED,
            f"the bundle was not attributed ({getattr(error, 'code', '')}): {error}",
        ) from error
    except (LandingError, BundleError) as error:
        raise DaemonRequestError(
            ipc_errors.TRANSFER_LANDING_FAILED, str(error)
        ) from error
    _apply_pins(registry, name, pins)
    return ReceivedTarget(
        plan=plan,
        landing=landing,
        half=TargetHalf(
            operation_id=operation_id,
            admission=store.get(operation_id),
            row=store.journal.get(operation_id),
        ),
    )
