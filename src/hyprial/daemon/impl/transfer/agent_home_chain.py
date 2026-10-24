"""The agent-home P2 chain as the operator (CLI) half drives it.

Split out of ``orchestrator.py`` when this head's fixes pushed that module over
the structural line limit; the P0 cold-migration sequence stays there.  This
module owns the chain AT07 wired: quiesce -> bundle -> ship -> receive ->
complete -- plus the journaled rollback of a failure that happened before the
target could have been touched.

Rules this half owns, from the chain's own doctrine and the adversarial review
of the first landed version:

* a failure **before** the receive call is issued may roll the source back
  through the audited override -- the target was certainly not touched;
* a failure **at or after** the receive call keeps the hold.  An unresolved
  half only holds: the target may already have landed the worker, and
  compensating would trade one unreadable half for two live workers;
* the one remote directory this flow owns is created before the ship and
  removed as soon as nothing can still be reading it (its path is named in the
  error when it deliberately stays);
* a completion that leaves the source half held is reported as a failure, not
  as ``ok``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.transfer.execution.ssh import RemoteError, RemoteRunner
from hyprial.daemon.impl.transfer.orchestrator import TransferError, _required

#: The daemon answers ``transfer.quiesce`` only after its own P2 wait
#: (``source_quiesce_deadline_seconds`` + 30s, declared in
#: ``contract/daemon-lifecycle/p2-source-bounds.json``).  The CLI's default IPC
#: round trip is 15s -- shorter than a legal slow stop, so a timeout there would
#: look like a transport failure while the daemon kept going.  Wait out the
#: daemon's own budget plus a margin for the round trip itself.
CHAIN_IPC_SECONDS = 130.0


def _chain_plan_digest(plan: dict[str, Any], name: str, target_node: str) -> str:
    """One SHA256 over the facts both halves of the chain bind.

    The digest is the chain's join field, so it only has to be the same string
    on both ends; deriving it from the plan (rather than inventing a random id)
    keeps it auditable after the fact.
    """

    body = {
        "actor": plan.get("actor"),
        "name": name,
        "nodeId": plan.get("nodeId"),
        "owner": plan.get("owner"),
        "targetNode": target_node,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _launch_wiring_identity(agent: object, harness: str) -> dict[str, object]:
    """The source registry row's launch wiring, as the bundle's identity bag.

    The P2 chain's transfer unit is the home, and the home carries no registry
    row: the model vendor, model and preferred harness live only in the source
    daemon's SQLite.  A landed row without them cannot be resolved into a
    launch, so a dispatched message sits with no worker.  The source stamps
    the one fact set the target cannot reconstruct into the sealed manifest
    identity bag.  Absent values are omitted, so a source row that never had
    them produces the same bundle as before this field existed.

    ``harness`` is the chain's own harness, never the row's
    ``preferredHarness``: an agent may legally have swapped harnesses
    (``preferred_harness`` and ``last_harness`` can differ), and the transfer's
    harness is the one its provider/model are being moved for.  Stamping the
    chain harness also keeps the landing-side gate from refusing a legitimately
    swapped agent (B1).
    """

    if not isinstance(agent, dict):
        return {}
    provider = agent.get("provider")
    model = agent.get("model")
    launch: dict[str, object] = {}
    if isinstance(provider, str) and provider:
        launch["provider"] = provider
    if isinstance(model, str) and model:
        launch["model"] = model
    if launch and isinstance(harness, str) and harness:
        launch["harness"] = harness
    return {"launch": launch} if launch else {}


def _agent_home_dry_run(
    *,
    name: str,
    harness: str,
    host: str,
    plan: dict[str, Any],
    remote_node: str,
    remote_facts: dict[str, Any],
    emit: Callable[[str], None],
) -> dict[str, Any]:
    """What a P2 transfer would do, without stopping anything.

    ``ok`` is a claim, so the dry run makes it only when the real call would
    make it: the fence is still the daemon's (it re-decides at quiesce), but
    the dry run answers from the same table the daemon reads.  Before ③-4 the
    CLI refused an agent-home worker outright -- under ``--dry-run`` too -- and
    that refusal must not have become "reports a plan that would be refused".
    """

    from hyprial.daemon.impl.transfer.p2.orchestration.lift import (
        P2_CHAIN_MODE,
        chain_is_proven,
    )

    digest = _chain_plan_digest(plan, name, remote_node)
    emit(f"plan: agent-home P2 chain for {harness}:{name} -> {host} ({remote_node})")
    if not chain_is_proven(harness):
        emit(f"refused: {harness} is not a harness a two-daemon run has proved")
        return {
            "ok": False,
            "dryRun": True,
            "refused": True,
            "code": ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            "mode": P2_CHAIN_MODE,
            "harness": harness,
            "name": name,
            "from": plan.get("actor"),
            "toHost": host,
            "toNode": remote_node,
            # The daemon answers the real call; the dry run only says which
            # answer the same table implies.
            "fence": "daemon-decided-at-quiesce",
        }
    return {
        "ok": True,
        "dryRun": True,
        "mode": "agent-home-p2",
        "harness": harness,
        "name": name,
        "from": plan.get("actor"),
        "toHost": host,
        "toNode": remote_node,
        "remote": remote_facts,
        "chain": {
            "planDigest": digest,
            "targetNode": remote_node,
            "targetActor": name,
        },
        # The capability fence is the daemon's call: `transfer.quiesce` refuses
        # a harness no two-daemon run has proved.  A dry run reports the plan it
        # would open and names who decides, instead of guessing.
        "fence": "daemon-decided-at-quiesce",
    }


def _discard_remote_bundle(
    remote: RemoteRunner, remote_dir: str, *, emit: Callable[[str], None], reason: str
) -> str | None:
    """Remove a shipped bundle; report, never mask, a cleanup that failed.

    Called only when nothing can still be reading the directory: after a
    successful receive, or on a failure the target never saw.  An unresolved
    receive keeps its bundle on purpose -- a landing that is still running (or
    a connection that died mid-flight) must not have its input deleted -- and
    the error names the path instead.
    """

    try:
        remote.remove_tree(remote_dir)
    except Exception as error:  # noqa: BLE001 - reported, never masked
        emit(f"warning: remote bundle {remote_dir} could not be removed ({reason}): {error}")
        return str(error)
    emit(f"removed remote bundle {remote_dir} ({reason})")
    return None


def _roll_back_agent_home_chain(
    local_request: Callable[..., Any],
    *,
    operation_id: str,
    actor: str,
    owner: str,
    spec: dict[str, Any],
    reason: str,
    target_node: str,
) -> tuple[dict[str, Any], str | None]:
    """Release the chain's hold (audited) and put the worker back where it was.

    The hold is a durable fence, so the rollback is an explicit owner admission
    rather than a boolean force: the override names the principal and a reason,
    and its journal row is the audit receipt.  The same audited act retires the
    operation's preparation, so the worker can be attempted again instead of
    staying reserved forever.  Both halves are reported, never retried -- a
    rollback that could not release the hold must be visible, not silently
    assumed.
    """

    from hyprial.daemon.impl.transfer.p2.journal import MAX_ATTENTION_REASON

    released: Any = None
    release_error: str | None = None
    try:
        released = local_request(
            "transfer.custody.override",
            {
                "principal": owner,
                "operationId": operation_id,
                "actor": actor,
                "reason": f"transfer rollback to {target_node}: {reason}"[
                    :MAX_ATTENTION_REASON
                ],
            },
            timeout=CHAIN_IPC_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - reported, never masked
        release_error = str(error)
    resumed: Any = None
    resume_error: str | None = None
    try:
        resumed = local_request(
            "transfer.resume", {"spec": spec}, timeout=CHAIN_IPC_SECONDS
        )
    except Exception as error:  # noqa: BLE001 - reported, never masked
        resume_error = str(error)
    detail = "; ".join(part for part in (release_error, resume_error) if part)
    return {"override": released, "resume": resumed}, detail or None


def _chain_error_code(error: BaseException) -> str:
    if isinstance(error, TransferError):
        return error.code
    if isinstance(error, RemoteError):
        return "TRANSFER_REMOTE_FAILED"
    return "TRANSFER_FAILED"


def _fetch_recipient_public_key(remote: RemoteRunner, host: str) -> bytes:
    """The target's X25519 public half, asked over the same remote channel.

    Fetched before the source is stopped and before anything is packed: the
    one thing this chain must never do is ship credentials in the clear.  A
    target that cannot answer leaves the worker untouched and the transfer
    refused.
    """

    try:
        reply = remote.run_json(["agent", "transfer", "recipient-key"])
    except RemoteError as error:
        raise TransferError(
            "TRANSFER_REMOTE_FAILED",
            f"{host} did not answer with its credential-envelope key "
            f"({error}); refusing to ship credentials unsealed",
        ) from error
    encoded = reply.get("publicKey") if isinstance(reply, dict) else None
    try:
        key = (
            base64.b64decode(encoded, validate=True)
            if isinstance(encoded, str)
            else b""
        )
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        raise TransferError(
            "TRANSFER_PROTOCOL",
            f"{host} answered an unusable credential-envelope public key; "
            "refusing to ship credentials unsealed",
        )
    return key


def _run_agent_home_chain(
    *,
    name: str,
    harness: str,
    host: str,
    plan: dict[str, Any],
    spec: dict[str, Any],
    destination_cwd: str,
    local_request: Callable[..., Any],
    remote: RemoteRunner,
    remote_node: str,
    remote_socket: str,
    remote_facts: dict[str, Any],
    old_actor: str,
    emit: Callable[[str], None],
    strict_timeout: float,
) -> dict[str, Any]:
    """Drive the P2 chain: quiesce, bundle, ship (bundle + worktree), receive, complete.

    The commit point is the target's landing -- its answer carries the report
    the source completes on -- exactly as the P0 sequence's is
    ``transfer-receive``.  Two transfer units leave the source in the
    pre-receive window: the bundle (the agent HOME) and, like P0's cold
    migration, the worker's working tree (D1: a clean target has no cwd for
    the resume, and pi's trust check needs its ``.git`` boundary).  The
    target's landing turns the bundle into a local agent.  The only failures
    that roll the source back are the ones that happened before the receive
    call was issued; everything after it holds.
    """

    from hyprial.daemon.impl.transfer.archive.attribution import (
        sign_exported_bundle,
    )
    from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
    from hyprial.daemon.impl.transfer.archive.envelope import (
        ENVELOPE_RELPATH,
        EnvCredential,
        envelope_json_bytes,
        plan_credential_seal,
        seal_credential_envelope,
    )

    operation_id = f"transfer-p2:{uuid4().hex}"
    digest = _chain_plan_digest(plan, name, remote_node)
    source_node = str(plan["nodeId"])

    # -- 5a. the target's credential-envelope key, before anything stops -----
    # A target that cannot name its half must not cost the worker its hold.
    recipient_public_key = _fetch_recipient_public_key(remote, host)

    # -- 5p. quiesce through the chain: the source half is recorded first, then
    #        the worker stops through the hold this chain owns.  The round trip
    #        outlasts the daemon's own quiesce budget, so a legal slow stop is
    #        answered by the daemon that owns it instead of timing out here.
    quiesced = local_request(
        "transfer.quiesce",
        {
            "name": name,
            "provider": harness,
            "operationId": operation_id,
            "targetNode": remote_node,
            "targetActor": name,
            "planDigest": digest,
        },
        timeout=CHAIN_IPC_SECONDS,
    )
    spec = dict(_required(quiesced.get("spec"), "spec"))
    chain = dict(_required(quiesced.get("chain"), "chain"))
    token = str(_required(chain.get("sourceEntityToken"), "chain.sourceEntityToken"))
    # The daemon's own home, not the CLI's configured one: the export must read
    # the tree the daemon serves, and a missing answer fails closed here
    # instead of shipping whatever the CLI's environment happens to name (B6).
    source_home = str(_required(chain.get("sourceHome"), "chain.sourceHome"))
    # Signed by the machine whose daemon answered, not by whoever ran the CLI:
    # the key lives in that home and never inside the bundle it stamps.
    signing_home = str(_required(chain.get("signingHome"), "chain.signingHome"))
    half = chain.get("quiesce") if isinstance(chain.get("quiesce"), dict) else {}
    state = str(half.get("state"))
    if state != "quiesced":
        # ``unknown`` and ``stop-not-proven`` are not "nothing happened": the
        # admission and the hold are recorded and the worker may or may not
        # have stopped.  Say exactly that, and name the release that exists.
        raise TransferError(
            ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
            f"the source half of {operation_id} reads {state!r} "
            f"(reason {half.get('reason')!r}), not 'quiesced': the admission and "
            "the hold are recorded and the worker may or may not have stopped. "
            "Nothing was shipped and nothing else will be done to this chain "
            f"automatically; release it with an audited transfer.custody.override "
            f"for {operation_id} (through a retry of this transfer) if the worker "
            "should run again",
            {"operationId": operation_id, "chain": chain},
        )
    emit(f"quiesced on {source_node} through chain {operation_id}")

    bundle_dir = Path(tempfile.mkdtemp(prefix="hyprial-transfer-bundle-"))
    # Stage beside the target daemon's own socket, so the landing reads what the
    # same user just wrote and no remote path has to be invented or guessed.
    incoming_root = str(Path(remote_socket).parent / "transfer-incoming")
    remote_bundle = str(Path(incoming_root) / operation_id)
    staged = False
    try:
        # -- 6p. one bundle out of the quiesced home, then ship it ------------
        # ``rsync -a`` creates the final component of its destination but not
        # the path above it: GNU rsync (a Linux target) then fails outright,
        # while the macOS openrsync the local rig runs creates the parents and
        # hid that.  Create the one remote directory this flow owns first.
        remote.make_dirs(incoming_root)
        staged = True
        # The daemon resolved the agent's authorized env-key credentials at
        # quiesce (grant + incarnation gate); the export seals exactly those.
        env_credentials = tuple(
            EnvCredential.from_dict(item)
            for item in (chain.get("envCredentials") or [])
        )
        seal_plan = plan_credential_seal(
            Path(source_home), env_credentials=env_credentials
        )
        created_at = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        envelope = seal_credential_envelope(
            seal_plan,
            actor=name,
            owner=str(_required(plan.get("owner"), "owner")),
            created_at=created_at,
            recipient_public_key=recipient_public_key,
        )
        export_bundle(
            Path(source_home),
            bundle_dir,
            owner=str(_required(plan.get("owner"), "owner")),
            actor=name,
            source_machine=source_node,
            created_at=created_at,
            session_refs=tuple(
                ref
                for ref in [spec.get("sessionRef")]
                if isinstance(ref, str) and ref
            ),
            source_epoch=token,
            identity=_launch_wiring_identity(quiesced.get("agent"), harness),
            credential_envelope=ENVELOPE_RELPATH,
            # The sealed files' plaintext stays out of the payload; their
            # values ride the envelope instead (exactly the excluded paths).
            exclude_paths=seal_plan.excluded,
            extra_files={
                ENVELOPE_RELPATH: (envelope_json_bytes(envelope), 0o600)
            },
        )
        emit(
            f"sealed {len(seal_plan.records)} credential(s) for {host}; "
            f"{len(seal_plan.excluded)} plaintext file(s) held back"
        )
        # Attribution: stamp the sealed digest with the source machine's own
        # Ed25519 key before the bytes leave.  A failure here is raised inside
        # the pre-receive window below, so it rolls the source back exactly like
        # a failed ship does -- nothing has been handed to the target yet.
        signed = sign_exported_bundle(
            bundle_dir,
            home=signing_home,
            credential_envelope=ENVELOPE_RELPATH,
        )
        emit(f"bundle {signed.key_id} -> {host}:{remote_bundle}")
        remote.sync_tree(bundle_dir, remote_bundle)
        # -- 6q. the worktree ships too (D1) ---------------------------------
        # The bundle above is the agent HOME; the worker's working tree is a
        # second transfer unit, exactly as P0's cold migration treats it
        # (``orchestrator.py``: one worktree rsync before the receive).  On a
        # clean target the cwd does not exist, so the daemon's
        # ``validate_harness_cwd`` refuses the resume before any harness
        # trust check; and pi's loader additionally needs the ``.git``
        # boundary the source tree carries.  Same rsync face as P0:
        # ``sync_tree`` is ``rsync -a`` with no ``--delete``, so a target cwd
        # that already holds files is merged into, never cleared.  Create the
        # destination first: GNU rsync creates the final component but not
        # the path above it.
        source_cwd = str(_required(spec.get("cwd"), "spec.cwd"))
        emit(f"rsync worktree {source_cwd} -> {host}:{destination_cwd}")
        remote.make_dirs(destination_cwd)
        remote.sync_tree(Path(source_cwd), destination_cwd)
    except BaseException as error:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        if staged:
            _discard_remote_bundle(
                remote, remote_bundle, emit=emit, reason="the ship failed"
            )
        rollback, rollback_error = _roll_back_agent_home_chain(
            local_request,
            operation_id=operation_id,
            actor=name,
            owner=str(plan.get("owner") or ""),
            spec=spec,
            reason=str(error),
            target_node=remote_node,
        )
        if rollback_error:
            # The override/resume pair did not complete: claiming the hold was
            # released would be false, and the operator's next move is not the
            # one a released hold would suggest.
            outcome = (
                f"the rollback did NOT complete ({rollback_error}): the source "
                f"hold may still stand. Release it with an audited "
                f"transfer.custody.override for {operation_id}, then resume the "
                "worker"
            )
        else:
            outcome = (
                "the source hold was released through the audited override and "
                "the worker resumed"
            )
        raise TransferError(
            _chain_error_code(error),
            f"transfer of {harness}:{name} to {host} failed before the target "
            f"could receive anything; {outcome}: {error}",
            {
                "rollback": rollback,
                "rollbackError": rollback_error,
                "stage": "bundle/ship",
                "operationId": operation_id,
            },
        ) from error
    finally:
        shutil.rmtree(bundle_dir, ignore_errors=True)

    # -- 7p. receive: the target admits its half, lands and activates --------
    # From the moment this call is issued the target may touch real state, so
    # nothing below compensates: only the pre-receive branch above rolls back.
    try:
        received = remote.run_json(
            ["agent", "transfer", "receive"],
            stdin=json.dumps(
                {
                    "operationId": operation_id,
                    "planDigest": digest,
                    "sourceNode": source_node,
                    "sourceActor": name,
                    "sourceEntityToken": token,
                    "targetActor": name,
                    "bundleDir": remote_bundle,
                    "spec": {**spec, "cwd": destination_cwd},
                    # The adapters the source row holds travel with the chain,
                    # exactly as P0's receive payload carries them: a re-pin is
                    # a MOVE, so the target re-applies each one behind the
                    # conflict gate.  Without this the transfer would drop the
                    # worker's adapter pins on the floor (B1).
                    "pins": list(
                        (quiesced.get("agent") or {}).get("pinnedAdapters") or []
                    ),
                    "strictTimeoutSeconds": strict_timeout,
                }
            ).encode(),
        )
        report = _required((received.get("chain") or {}).get("report"), "chain.report")
        new_actor = str(_required(received.get("actor"), "actor"))
        phases = ",".join(str(item) for item in (received.get("phases") or []))
        emit(f"received on {remote_node}: {new_actor} ({phases})")
    except BaseException as error:
        # The bundle is NOT removed here: the landing may still be running (or
        # the connection may have died mid-flight), and deleting its input
        # would turn a recoverable half into a lost one.  Name the path.
        raise TransferError(
            _chain_error_code(error),
            f"transfer of {harness}:{name} to {host} is UNRESOLVED after the "
            "receive call was issued: the target may already have landed this "
            f"worker, so the source hold on {source_node} is kept -- an "
            "unresolved half only holds, because releasing it could leave two "
            "live workers. Inspect the target (its own chain row for "
            f"{operation_id}); to release the source half use the audited "
            "transfer.custody.override through a retry of this transfer, then "
            f"resume it. The shipped bundle stays at {remote_bundle} for that "
            f"inspection: {error}",
            {
                "operationId": operation_id,
                "chain": chain,
                "stage": "receive-unresolved",
                "bundleDir": remote_bundle,
                "sourceHold": "kept",
            },
        ) from error
    else:
        _discard_remote_bundle(remote, remote_bundle, emit=emit, reason="received")

    # -- 8p. complete (source cleanup; never rolls back, never hides a hold) ---
    try:
        completed: dict[str, Any] | None = local_request(
            "transfer.complete",
            {
                "name": name,
                "provider": harness,
                "operationId": operation_id,
                "chain": {"report": report},
            },
            timeout=CHAIN_IPC_SECONDS,
        )
        complete_error: str | None = None
    except Exception as error:  # noqa: BLE001 - the worker is already live
        completed, complete_error = None, str(error)
    decision = completed.get("chain") if isinstance(completed, dict) else None
    action = decision.get("action") if isinstance(decision, dict) else None
    held = (
        complete_error is not None
        or not isinstance(completed, dict)
        or completed.get("ok") is not True
        or action != "complete"
    )
    if held:
        reason = (
            complete_error
            or (decision or {}).get("reason")
            or (completed or {}).get("error")
            or "the source half did not reach the complete decision"
        )
        emit(
            f"warning: target has {name!r} live, but the source half is still "
            f"held: {reason}"
        )
    return {
        "ok": not held,
        **({"error": f"source half still held: {reason}"} if held else {}),
        "mode": "agent-home-p2",
        "harness": harness,
        "name": name,
        "from": old_actor,
        "to": new_actor,
        "remote": remote_facts,
        "toHost": host,
        "toNode": remote_node,
        "cwd": destination_cwd,
        "sessionRef": spec.get("sessionRef"),
        "sandboxSmoke": received.get("sandboxSmoke"),
        "source": completed,
        "chain": {
            "operationId": operation_id,
            "planDigest": digest,
            "phases": received.get("phases"),
            "action": action,
            "targetPhase": (decision or {}).get("targetPhase"),
            "sourceHold": "kept" if held else None,
        },
    }
