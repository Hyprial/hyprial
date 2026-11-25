# Decision record (W2B-2, 2026-11-21):
# - _publish_session_ref already allocates the staging file via tempfile.mkstemp
#   (0o600 on POSIX) inside the guarded state directory under process umask, so
#   the redundant temporary.chmod(0o600) is removed without growing CLI->backend
#   import ratchet edges.
"""Shared interactive-session launch runtime (context, custody, TUI)."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Sequence
from pathlib import Path
from contextlib import contextmanager
from hyprial.error import codes
from hyprial.kernel import agent_uri_actor, canonical_agent_uri, resolve_node_id
import json
import os
import re
import signal
import subprocess
import tempfile


from hyprial.shell.impl.cli.commands.common.support import JsonObject, _append_warning


def _preflight_codex_credential(
    projection: JsonObject | None, *, name: str, model_provider: str | None
) -> None:
    if projection is None:
        return
    from hyprial.identity import (
        AgentRuntimeError,
        SharedCredentialBinding,
        preflight_codex_credential,
    )

    shared = projection.get("sharedCredential")
    binding = (
        SharedCredentialBinding(
            actor=agent_uri_actor(name) or name,
            harness="codex",
            native_path=Path(str(shared["nativePath"])),
            target_path=Path(str(shared["targetPath"])),
            agent_cwd=None,
        )
        if isinstance(shared, dict)
        else None
    )
    try:
        preflight_codex_credential(
            native_root=Path(str(projection["nativeRoot"])),
            actor=agent_uri_actor(name) or name,
            model_provider=model_provider,
            shared_credential=binding,
        )
    except AgentRuntimeError as error:
        raise get_services().CliError(
            error.code or codes.INVALID_ARGUMENT,
            str(error),
            data=error.data,
        ) from error


def _plugin_skip_warnings(skipped: Sequence[Any]) -> list[JsonObject]:
    return [
        {
            "code": "PLUGIN_SKIPPED",
            "message": f"plugin {item.name!r} was not loaded: {item.reason}",
            "data": {"plugin": item.name, "reason": item.reason},
        }
        for item in skipped
    ]


def _announce_plugin_skips(warnings: Sequence[JsonObject]) -> None:
    for warning in warnings:
        warn(f"hyprial: warning: {warning['message']}", json_output=False)


def _with_plugin_warnings(
    result: JsonObject, warnings: Sequence[JsonObject]
) -> JsonObject:
    for warning in warnings:
        _append_warning(result, warning)
    return result


def _publish_session_ref(path: Path, session_ref: str) -> None:
    """Atomically publish one owner-only session ref for child CLI calls."""

    if not session_ref or any(character.isspace() for character in session_ref):
        raise ValueError("session ref must be one non-empty token")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(session_ref)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _interactive_cli_identity(actor: str, session_ref_file: Path) -> dict[str, str]:
    """Child-only environment that lets CLI calls present a daemon fence."""

    return {
        "HYPRIAL_AGENT_SESSION": "1",
        "HYPRIAL_WORKER_ACTOR": actor,
        "HYPRIAL_WORKER_SESSION_REF_FILE": str(session_ref_file.absolute()),
    }


def _wait_foreground(process: subprocess.Popen[Any]) -> int:
    """Wait for the interactive TUI while leaving Ctrl-C to the child.

    The child shares this process's terminal and process group, so SIGINT
    already reaches the TUI directly. Ignoring SIGINT here keeps a turn
    interrupt inside Claude from tearing the whole session down through this
    supervisor's error path.
    """

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return process.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _interactive_actor(name: str, status: JsonObject) -> str:
    """Mint the canonical ``agent:<owner>:<machine>:<actor>`` for ``--name``.

    Pins, harnesses, and delivery addressing all speak the four-segment
    URI, and launch-time code that must *compare* against daemon truth
    (registration polling, result payloads) needs the
    minted spelling. The daemon's ``ps`` response is the source of truth for
    owner and node id, with the daemon's own fallbacks
    (``HYPRIAL_OWNER``/login, ``HYPRIAL_NODE_ID``/home node-id file) mirrored for older
    daemons that do not report them yet. A name that already is a
    four-segment URI passes through; any other colon form is rejected
    loudly instead of registering a third spelling.

    ⛔ Never persist this value: a minted address is derived state, and
    freezing it into an mcp-config or argv is what stranded still-running
    channels on a dead owner segment after the #352 owner switch. Anything
    that outlives the launch must carry the bare name
    (:func:`_interactive_actor_name`) and let the daemon mint per call.
    """
    services = get_services()

    if ":" not in name:
        from hyprial.daemon import resolve_node_owner

        daemon = status.get("daemon") if isinstance(status, dict) else None
        node_id = ""
        owner = ""
        if isinstance(daemon, dict):
            node_id = str(daemon.get("nodeId") or "").strip()
            owner = str(daemon.get("owner") or "").strip()
        if not node_id:
            node_id = resolve_node_id()
        if not owner:
            owner = resolve_node_owner()
        return _registered_id_address(name, status, owner, node_id) or (
            canonical_agent_uri(owner, node_id, name)
        )
    if agent_uri_actor(name) is not None:
        return name
    raise services.CliError(
        codes.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


def _registered_id_address(
    name: str, status: JsonObject, owner: str, node_id: str
) -> str | None:
    """The ``a.`` id URI the daemon's ``ps`` reports for local agent ``name``.

    The daemon registers a registered agent's interactive session at the
    URI its record stores; once records are minted under their id that is
    not the name-composed URI, so the launcher must compare against the
    record.  Only an id-form URI under this daemon's own owner and node is
    taken; every other case keeps the name-composed address.
    """

    rows = status.get("agents") if isinstance(status, dict) else None
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("actor") != name:
            continue
        if row.get("owner") != owner or row.get("machine", node_id) != node_id:
            continue
        uri = row.get("uri")
        if isinstance(uri, str) and uri:
            return uri
    return None


def _official_session_address(actor: str, status: JsonObject) -> str:
    """The address the daemon's ``ps`` registers ``actor``'s session at.

    Integrator decision D12: a launcher may mint the name URI before the
    record exists (its first ``session.register`` creates it), while the
    daemon registers the session at the URI the record stores, the ``a.``
    id URI once records are id-minted.  The ``ps`` agents row whose own
    owner, node and name compose ``actor`` gives that address (a row without
    a machine is on the daemon's reported node); anything else keeps
    ``actor``.  The URI is never decomposed here: only the URI module reads
    its segments.
    """

    name = agent_uri_actor(actor)
    if name is None:
        return actor
    daemon = status.get("daemon") if isinstance(status, dict) else None
    daemon_node = daemon.get("nodeId") if isinstance(daemon, dict) else None
    rows = status.get("agents") if isinstance(status, dict) else None
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict) or row.get("actor") != name:
            continue
        owner = row.get("owner")
        node_id = row.get("machine", daemon_node)
        if not isinstance(owner, str) or not isinstance(node_id, str):
            continue
        try:
            composed = canonical_agent_uri(owner, node_id, name)
        except ValueError:
            continue
        if composed != actor:
            continue
        uri = row.get("uri")
        if isinstance(uri, str) and uri:
            return uri
    return actor


def _interactive_actor_name(name: str) -> str:
    """Reduce ``--name`` to the bare actor name; validation only, never mint.

    The value that outlives a launch -- the mcp-config JSON and the channel
    argv -- is the operator's input, not a derived address: the daemon's
    boundary (``_canonical_interactive_actor``) mints the canonical
    four-segment URI per call from its own current owner/node, so an owner
    change never strands a still-running channel on a stale spelling. A
    four-segment URI still passes validation and reduces to its short name.
    """
    services = get_services()

    if ":" not in name:
        return name
    short = agent_uri_actor(name)
    if short is not None:
        return short
    raise services.CliError(
        codes.INVALID_ARGUMENT,
        f"--name {name!r} is neither a bare actor name nor a canonical "
        "agent:<owner>:<machine>:<actor> URI",
    )


def _handover_prompt(result: JsonObject) -> str | None:
    """The A9 notice to put in front of the agent's first turn, if any."""

    handover = result.get("harnessHandover")
    if not isinstance(handover, dict):
        return None
    notice = handover.get("notice")
    return notice if isinstance(notice, str) and notice else None


def _runtime_context_environment(
    *,
    name: str,
    harness: str,
    cwd: Path,
    json_output: bool,
    include_projection: bool = False,
) -> JsonObject | None:
    projection = _runtime_context_projection(
        name=name, harness=harness, cwd=cwd, json_output=json_output
    )
    if projection is None:
        return None
    return (
        dict(projection)
        if include_projection
        else dict(projection["environment"])
    )


def _runtime_context_projection(
    *,
    name: str,
    harness: str,
    cwd: Path,
    json_output: bool,
    model_provider: str | None = None,
    credential_preflight: bool = False,
) -> JsonObject | None:
    """Ask the daemon for one non-secret P2 root/profile projection.

    ``None`` is the explicit legacy mode.  The response is intentionally a
    string map: entity tokens, grants, credential values, and receipts do not
    cross in this projection. Credential values cross only in the later
    acquire reply, and only for the one child protected by its spawn lease.
    An opaque launch token authorizes the final incarnation and grant checks.
    Each daemon notice is warned on stderr; ``json_output`` is the caller's
    real value, so ``warn`` keeps its own --json contract (#2600).
    """
    services = get_services()

    result = services.runtime.request(
        "agent.runtime-context",
        {
            "name": name,
            "harness": harness,
            "cwd": str(cwd),
            **(
                {"modelProvider": model_provider}
                if model_provider is not None
                else {}
            ),
            **(
                {"credentialPreflight": True}
                if credential_preflight
                else {}
            ),
        },
    )
    if result.get("mode") == "legacy":
        return None
    if result.get("mode") != "agent-home-p2":
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "daemon returned an unsupported agent runtime context mode",
        )
    environment = result.get("environment")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "daemon returned an invalid agent runtime environment",
        )
    for field in ("projectionRoot", "nativeRoot", "sessionRoot"):
        value = result.get(field)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                f"daemon returned an invalid agent runtime {field}",
            )
    notices = result.get("notices", [])
    if not isinstance(notices, list):
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "daemon returned invalid Agent runtime notices",
        )
    for notice in notices:
        if (
            not isinstance(notice, dict)
            or not isinstance(notice.get("code"), str)
            or not isinstance(notice.get("actor"), str)
            or not isinstance(notice.get("message"), str)
            or not isinstance(notice.get("path"), str)
            or not Path(notice["path"]).is_absolute()
        ):
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "daemon returned an invalid Agent runtime notice",
            )
        warn(notice["message"], json_output=json_output)
    shared_credential = result.get("sharedCredential")
    if shared_credential is not None:
        if not isinstance(shared_credential, dict) or shared_credential.get(
            "authMode"
        ) != "native-shared-link":
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "daemon returned an invalid shared credential binding",
            )
        for field in ("nativePath", "targetPath"):
            value = shared_credential.get(field)
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    f"daemon returned an invalid shared credential {field}",
                )
    return result


@contextmanager
def _runtime_launch_custody(projection: JsonObject | None):
    services = get_services()
    if projection is None or projection.get("authorityPrepared") is not True:
        yield {}
        return
    launch_token = projection.get("launchToken")
    if not isinstance(launch_token, str) or not launch_token:
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "authority-prepared runtime context is missing launchToken",
        )
    operation_id = f"cli:runtime-launch:{services.uuid4().hex}"
    acquired = services.runtime.request(
        "agent.runtime-launch.acquire",
        {"launchToken": launch_token, "operationId": operation_id},
    )
    if acquired.get("operationId") != operation_id:
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "daemon returned a mismatched Agent runtime launch operation",
        )
    lease_token = acquired.get("leaseToken")
    if not isinstance(lease_token, str) or not lease_token:
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            "daemon returned an invalid Agent runtime launch lease",
        )
    body_failed = False
    try:
        raw_secret_environment = acquired.get("secretEnvironment", {})
        if not isinstance(raw_secret_environment, dict) or any(
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None
            or not isinstance(value, str)
            for name, value in raw_secret_environment.items()
        ):
            raise services.CliError(
                codes.INVALID_ARGUMENT,
                "daemon returned an invalid Agent launch secret environment",
            )
        secret_environment = dict(raw_secret_environment)
        yield secret_environment
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            services.runtime.request(
                "agent.runtime-launch.release",
                {"leaseToken": lease_token, "operationId": operation_id},
            )
        except Exception:
            if not body_failed:
                raise


def _channel_registration_confirmed(
    actor: str, session_ref: str, status: JsonObject
) -> bool:
    """Readiness for the Claude Channel carrier: registered AND confirmed.

    The session is compared at its official address (D12), as for the pi and
    Codex carriers: the daemon registers the Channel session at the URI the
    record stores (its ``a.`` id URI once records are id-minted), never at a
    name URI the launcher holds (``--name agent:<owner>:<node>:<actor>``).
    """

    official = _official_session_address(actor, status)
    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == official
        and item.get("sessionRef") == session_ref
        and item.get("channelConfirmed") is True
        for item in sessions
    )


def _pi_attach_registration(actor: str, session_ref: str, status: JsonObject) -> bool:
    """Readiness for the pi attach carrier: its session.register has landed.

    The session is compared at its official address (D12), not at the name
    URI the CLI minted before the record existed.
    """

    official = _official_session_address(actor, status)
    sessions = status.get("interactiveSessions", [])
    return isinstance(sessions, list) and any(
        isinstance(item, dict)
        and item.get("actor") == official
        and item.get("sessionRef") == session_ref
        and item.get("source") == "pi-extension"
        for item in sessions
    )


def _write_launch_config(config_path: Path, config: JsonObject) -> None:
    """Write the launch-scoped mcp config, mode 0600, refusing overwrites."""

    descriptor = os.open(
        config_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
