"""The receive pipeline: verify, unpack, land, activate, start, undo in reverse.

Decisions:

- Order is ``finalize`` -> ``unpack_stream`` -> ``validate_bundle`` plus the
  ``manifest_digest`` comparison -> ``land`` -> ``activate`` -> ``start``.
- Routines are created paused between ``activate`` and ``start``; each
  registers its ``remove`` right after its ``create_paused`` returns, so undo
  (stop, routines newest first, deactivate, undo landing) matches section 3.
- Every undo step is registered before the step it compensates runs, so a step
  that fails half way is compensated too (activation and start can leave
  partial effects).  ``land`` registers nothing before it: the registry landing
  undoes itself.  The undo stack runs in reverse and an undo step that raises
  never stops the rest nor changes the reply code.
- A ``TeleportError`` raised by a port keeps its code.  Any other exception is
  ``LANDING_FAILED`` from ``land``/``activate`` and ``STRICT_RESUME_FAILED``
  from ``start``.
- ``start`` returning a ref different from a non-null requested ``sessionRef``
  is ``STRICT_RESUME_FAILED``.  For a null request the returned ref is echoed.
- A bundle failing validation or the manifest digest is ``DIGEST_MISMATCH``.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.protocol import (
    ReceiveReply,
    ReceiveRequest,
    RoutineRestore,
)
from hyprial.daemon.impl.teleport.staging import StagingReceiver
from hyprial.daemon.impl.teleport.stream import unpack_stream
from hyprial.daemon.impl.teleport.target.ports import Landed, TargetHost, TargetRoutines
from hyprial.daemon.impl.transfer.archive.bundle import BundleError, validate_bundle

BUNDLE_DIRNAME = "bundle"


def _port_error(error: Exception, fallback: str, what: str) -> TeleportError:
    if isinstance(error, TeleportError):
        return error
    return TeleportError(fallback, f"{what} failed: {type(error).__name__}: {error}")


def unpack_and_verify(
    receiver: StagingReceiver, request: ReceiveRequest
) -> Path:
    operation_id = request.operation_id
    stream = receiver.finalize(operation_id, stream_sha256=request.stream_sha256)
    bundle_dir = Path(receiver.root) / operation_id / BUNDLE_DIRNAME
    try:
        unpack_stream(stream, bundle_dir, total=receiver.persisted(operation_id))
        try:
            manifest = validate_bundle(bundle_dir)
        except BundleError as error:
            raise TeleportError("TELEPORT_DIGEST_MISMATCH", f"bundle refused: {error}") from error
        sealed = manifest.as_dict().get("manifest_digest")
        if sealed != request.manifest_digest:
            raise TeleportError(
                "TELEPORT_DIGEST_MISMATCH", "manifest digest differs from the announced one"
            )
    except BaseException:
        shutil.rmtree(bundle_dir, ignore_errors=True)
        raise
    return bundle_dir


def land_and_start(
    host: TargetHost,
    bundle_dir: Path,
    *,
    name: str,
    harness: str,
    request: ReceiveRequest,
    on_undo: Callable[[Callable[[], None]], None],
    routines: TargetRoutines | None = None,
    source_uri: str = "",
) -> ReceiveReply:
    try:
        landed = host.land(bundle_dir, name=name, harness=harness)
    except Exception as error:
        raise _port_error(error, "TELEPORT_LANDING_FAILED", "landing") from error
    on_undo(lambda: host.undo_landing(landed))
    on_undo(lambda: host.deactivate(landed))
    try:
        host.activate(landed)
    except Exception as error:
        raise _port_error(error, "TELEPORT_LANDING_FAILED", "activation") from error
    restores = _create_routines(routines, landed, request, source_uri, on_undo)
    session_ref = _start(host, landed, request, on_undo)
    return ReceiveReply(
        operation_id=request.operation_id,
        agent_uri=landed.uri,
        session_ref=session_ref,
        routines=restores,
    )


def _create_routines(
    routines: TargetRoutines | None,
    landed: Landed,
    request: ReceiveRequest,
    source_uri: str,
    on_undo: Callable[[Callable[[], None]], None],
) -> tuple[RoutineRestore, ...]:
    if not request.routines:
        return ()
    if routines is None:
        raise TeleportError(
            "TELEPORT_RUNTIME_UNSUPPORTED", "this node has no routine runtime"
        )
    restores: list[RoutineRestore] = []
    for item in request.routines:
        try:
            created = routines.create_paused(
                yaml_text=item.yaml, source_uri=source_uri, target_uri=landed.uri
            )
        except Exception as error:
            raise _port_error(error, "TELEPORT_LANDING_FAILED", "routine creation") from error
        on_undo(lambda name=created: routines.remove(name))
        restores.append(RoutineRestore(item.name, restore_enabled=item.enabled))
    return tuple(restores)


def _start(
    host: TargetHost,
    landed: Landed,
    request: ReceiveRequest,
    on_undo: Callable[[Callable[[], None]], None],
) -> str | None:
    on_undo(lambda: host.stop(landed))
    try:
        established = host.start(
            landed,
            args=request.args,
            session_ref=request.session_ref,
            timeout=request.strict_timeout_seconds,
        )
    except Exception as error:
        raise _port_error(error, "TELEPORT_STRICT_RESUME_FAILED", "start") from error
    if request.session_ref is not None and established != request.session_ref:
        raise TeleportError(
            "TELEPORT_STRICT_RESUME_FAILED",
            f"expected session {request.session_ref!r}, established {established!r}",
        )
    return established
