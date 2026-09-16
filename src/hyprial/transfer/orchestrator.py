"""The transfer orchestrator: P0 cold migration, end to end.

Sequence (design ``notes/design-hyprial-transfer.md`` §2.2):

1. plan — read-only local inspection (spec, agent, pins, unread inbox);
2. fencing — target must be discovered; remote node must match that row;
   source/target node ids MUST differ, bare-mode owners MUST match;
3. precheck — target admission (harness transferable, name conflicts);
4. preflight — collect all target toolchain and both-end Docker/image gaps;
5. quiesce — stop the worker, snapshot with the freshest sessionRef;
6. ship — rsync the worktree, scp the harness session file(s);
7. receive — target adopts identity + spec and STRICT-resumes the session;
8. complete — source retires the old identity (inbox rows stay, P0).

The commit point is step 7's success.  Any failure after quiesce rolls the
source back with ``transfer.resume`` — the worker comes back where it was,
with its ref, and the error says so.  A failure IN complete is reported
loudly but never rolls back: the worker is already live on the target and
killing it would trade a cosmetic leftover for real downtime.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from hyprial.contracts import ipc_errors
from hyprial.transfer.container import (
    ContainerError,
    DockerRunner,
    credential_bundle,
    default_image,
)
from hyprial.transfer.session_files import (
    SessionFileError,
    SessionFileNotFound,
    locate_session_file,
    rewrite_pi_session_cwd,
)
from hyprial.transfer.ssh import RemoteError, RemoteRunner

#: The binaries each harness needs on the target's PATH.  claude's
#: connector launches its SDK worker through ``uv run --isolated``
#: (``agent_sdk.sdk_worker_command``), so uv is a hard prerequisite — the
#: docker pseudo-machine verification caught a target without it hanging
#: to the strict-resume timeout.
HARNESS_BINARIES = {
    "pi": ("pi",),
    "codex": ("codex",),
    "claude": ("claude", "uv"),
}


class TransferError(RuntimeError):
    """Orchestration-level failure with a stable code for the CLI."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.code = code
        self.data = data
        super().__init__(message)


def _required(value: Any, label: str) -> Any:
    if value is None:
        raise TransferError("TRANSFER_PROTOCOL", f"remote answer lacks {label}")
    return value


def _discovered_node(host: str, response: Any) -> str:
    """Resolve user@nodeId against this home's actual hosts schema.

    Host rows contain nodeId/status, NOT owner. Do not infer node identity
    from DNS/SSH aliases or infer daemon ownership from agents on that node.
    """
    parts = host.split("@")
    if (
        len(parts) > 2 or any(not part or part.startswith("-") for part in parts)
        or any(character.isspace() for character in host)
    ):
        raise TransferError("TRANSFER_TARGET_INVALID", f"invalid SSH destination {host!r}")
    node = parts[-1]
    rows = response.get("hosts") if isinstance(response, dict) else None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("nodeId"), str)
        or not row["nodeId"] for row in rows
    ):
        raise TransferError("TRANSFER_PROTOCOL", "local hyprial hosts must report nodeId rows")
    matches = [row for row in rows if row["nodeId"] == node]
    if not matches:
        raise TransferError(
            "TRANSFER_TARGET_UNDISCOVERED",
            f"target {host!r} is not in this home's hyprial hosts; use "
            "--to [user@]<exact discovered nodeId> (SSH aliases are not inferred)",
        )
    if len(matches) != 1:
        raise TransferError(
            "TRANSFER_TARGET_AMBIGUOUS", f"hyprial hosts has multiple rows for {node!r}"
        )
    return matches[0]["nodeId"]


def _local_login_user() -> str:
    # SSH's default account is an OS fact, not HYPRIAL_OWNER or an inferred
    # home-directory segment. Read the current UID, not spoofable USER env.
    return pwd.getpwuid(os.getuid()).pw_name


def _resolve_destination_cwd(
    explicit: str | None, source: Any, remote: RemoteRunner
) -> str:
    if explicit is not None:
        if not explicit.strip():
            raise TransferError("TRANSFER_CWD_REQUIRED", "--cwd must not be empty")
        return explicit
    if not source:
        raise TransferError("TRANSFER_PROTOCOL", "the spec carries no cwd; pass --cwd explicitly")
    try:
        source_user = _local_login_user()
        target_user = remote.login_user()
    except (RemoteError, OSError, KeyError) as error:
        raise TransferError(
            "TRANSFER_CWD_REQUIRED",
            f"cannot verify SSH login users for the same-path default: {error}; "
            "pass --cwd explicitly for the target",
        ) from error
    if (
        not isinstance(source_user, str) or not source_user
        or not isinstance(target_user, str) or not target_user
        or source_user != target_user
    ):
        raise TransferError(
            "TRANSFER_CWD_REQUIRED",
            f"same-path cwd default refused: source OS login={source_user!r}, "
            f"target SSH login={target_user!r}; pass --cwd with an explicit "
            "target path (no HOME mapping is inferred)",
            {"sourceUser": source_user, "targetUser": target_user, "sourceCwd": source},
        )
    return str(source)


def _preflight(
    *,
    host: str,
    harness: str,
    remote: RemoteRunner,
    docker: DockerRunner,
    containerized: bool,
    image: str,
    home: Path,
    with_credentials: bool,
    cwd: str,
    session_ref: str | None,
) -> Path | None:
    """Read-only prerequisite sweep; return the resolved source session file.

    #417 requires uv/claude/pi on the target's non-interactive SSH PATH in
    both modes. Bare codex also retains its own CLI prerequisite. Docker
    and the source image are additional requirements only in container mode.
    Claude's required credential file joins the sweep when shipping credentials;
    use the existing manifest validator (metadata only, no Keychain access).
    A persisted sessionRef must resolve too, even when infrastructure is missing.
    Identity fencing has already succeeded; no worker has been stopped.
    """
    missing: list[str] = []
    session_file: Path | None = None
    binaries = dict.fromkeys(
        ("uv", "claude", "pi")
        + (("docker",) if containerized else HARNESS_BINARIES[harness])
    )
    for binary in binaries:
        label = f"target {host}: {binary}"
        try:
            present = remote.has_binary(binary)
        except RemoteError as error:
            missing.append(f"{label} could not be checked: {error}")
            continue
        if not present:
            location = (
                "PATH or known install paths"
                if binary == "docker"
                else "non-interactive SSH PATH"
            )
            missing.append(f"{label} missing from {location}")
    if containerized:
        # info verifies both the local executable and access to its daemon;
        # an image inspect alone used to misreport no Docker as no image.
        try:
            docker.run(["info"])
        except (ContainerError, OSError, subprocess.TimeoutExpired) as error:
            missing.append(f"source docker unavailable: {error}")
        # Attempt this even when info fails: report missing/unverifiable
        # images alongside all other gaps, without claiming a proven absence.
        image_problem = (
            f"source worker image {image!r} is absent or cannot be inspected; "
            "build it on the source with docker/build-worker-image.sh"
        )
        try:
            if not docker.image_present(image):
                missing.append(image_problem)
        except (ContainerError, OSError, subprocess.TimeoutExpired) as error:
            missing.append(f"{image_problem}: {error}")
        if with_credentials and harness == "claude":
            # Do this even if either end lacks Docker or the image. The
            # manifest check reads no contents and supplies the required
            # operator-authorized export guidance for macOS Keychain users.
            try:
                credential_bundle(harness, home)
            except ContainerError as error:
                missing.append(f"source claude credentials: {error}")
            except OSError as error:
                missing.append(
                    f"source claude credentials could not be checked under {home}: "
                    f"{error}; any required export must be operator-authorized"
                )
    if session_ref:
        try:
            session_file = locate_session_file(harness, cwd, str(session_ref), home=home)
            if not session_file.is_file():
                raise SessionFileError(f"not a regular session file: {session_file}")
        except Exception as error:
            problem = f"source {harness} session file for ref {session_ref!r}: {error}"
            if isinstance(error, SessionFileNotFound):
                problem += (
                    "; if the source worker has never run, 先跑一个 turn "
                    "(run one turn on the source worker first), then retry transfer; "
                    "otherwise check or restore the missing transcript"
                )
            missing.append(problem)
    if missing:
        raise TransferError(
            ipc_errors.TRANSFER_PREREQUISITE,
            "transfer preflight found missing or unverified prerequisites:\n- "
            + "\n- ".join(missing),
            {"missing": missing},
        )
    return session_file


def run_transfer(
    *,
    name: str,
    harness: str | None,
    host: str,
    target_cwd: str | None,
    dry_run: bool,
    strict_timeout: float,
    local_request: Callable[..., Any],
    remote: RemoteRunner,
    home: Path | None = None,
    emit: Callable[[str], None] = lambda message: None,
    containerized: bool = False,
    container_image: str | None = None,
    with_credentials: bool = True,
    docker: DockerRunner | None = None,
    yes: bool = False,
) -> dict[str, Any]:
    """Move one managed headless worker to ``host``.  See module docstring.

    ``containerized`` selects the container mode (design
    ``docs/design-transfer-container.md``): the worker lands inside a
    docker container carrying the ORIGINAL owner's credentials (pinned
    owner URI, per-worker credential volume), the image ships as a
    ``docker save`` tar through the same transport (decision D-C), and
    the owner-match fencing is replaced by the pinned-owner rule.
    """

    home = Path.home() if home is None else Path(home)
    docker = DockerRunner() if docker is None else docker
    params: dict[str, Any] = {"name": name}
    if harness is not None:
        # The wire key stays "provider" (schemaVersion=1 dual-read rule).
        params["provider"] = harness

    # Discovery is a local daemon fact, not a fresh tailnet/DNS/SSH lookup.
    # Reject unknown destinations before invoking anything remotely.
    expected_node = _discovered_node(host, local_request("hosts", {}))

    # -- 1. plan (local, read-only) -----------------------------------------
    plan = local_request("transfer.plan", params)
    spec = dict(_required(plan.get("spec"), "spec"))
    # From here on, ``harness`` is the RESOLVED harness, not the CLI filter.
    harness = str(spec["provider"])
    old_actor = str(plan["actor"])
    emit(f"plan: {harness}:{name} on {plan['nodeId']} ({old_actor})")
    if plan.get("unreadInbox"):
        emit(
            f"note: {plan['unreadInbox']} undrained inbox row(s) stay on "
            f"{plan['nodeId']} under the old URI (P0 keep-inbox)"
        )

    # -- 2. fencing ----------------------------------------------------------
    remote_ps = remote.run_json(["ps"])
    remote_daemon = remote_ps.get("daemon")
    remote_node = remote_daemon.get("nodeId") if isinstance(remote_daemon, dict) else None
    # Narrow #417 ruling: discovery fences NODE only. This must precede
    # the existing P0 owner rule, including in pinned-owner container mode.
    if remote_node != expected_node:
        raise TransferError(
            "TRANSFER_TARGET_MISMATCH",
            f"target {host!r} identity mismatch: hyprial hosts nodeId={expected_node!r}; "
            f"remote daemon.nodeId={remote_node!r}; refusing transfer",
            {"hostsNodeId": expected_node, "remoteNodeId": remote_node},
        )
    remote_owner = str(_required(remote_daemon.get("owner"), "daemon.owner"))
    if remote_node == plan["nodeId"]:
        raise TransferError(
            "TRANSFER_NODE_CONFLICT",
            f"target {host} runs node {remote_node!r} — the SAME node id as "
            "the source; two daemons with one node id would mint identical "
            "actor URIs (dual-active). Set a distinct HYPRIAL_NODE_ID.",
        )
    if remote_owner != plan["owner"] and not containerized:
        raise TransferError(
            "TRANSFER_OWNER_MISMATCH",
            f"target owner {remote_owner!r} != source owner {plan['owner']!r}; "
            "moving across owners is a rename, not a transfer (P0 refuses)",
        )
    if containerized and remote_owner != plan["owner"]:
        # Container mode pins the SOURCE owner into the spec (decision
        # D-D): the daemons' ambient owners may differ -- the landed
        # worker keeps the original owner's identity anyway.
        emit(
            f"note: target daemon owner {remote_owner!r} differs; the "
            f"worker is pinned to owner {plan['owner']!r} (containerized)"
        )

    # B-narrow: report only facts we have, not inferred remote paths/HOME.
    # A socket may live outside <home>/state, so keep its exact reported value.
    remote_socket = remote_daemon.get("socket")
    if not isinstance(remote_socket, str) or not remote_socket.startswith("/"):
        raise TransferError(
            "TRANSFER_PROTOCOL",
            f"remote daemon.socket must report an absolute path; got {remote_socket!r}",
        )
    remote_facts = {
        "host": host,
        "remoteHyprial": remote.remote_hyprial,
        "daemonSocket": remote_socket,
        "actor": old_actor,
    }
    emit(f"target: will run {remote.remote_hyprial!r} on {host}")
    emit(f"remote daemon.socket: {remote_socket}")
    emit(f"transfer actor: {old_actor}")
    if not yes:
        raise TransferError(
            "TRANSFER_CONFIRMATION_REQUIRED",
            "transfer stopped without changing the worker: review the remote "
            "command, daemon.socket and actor above, then pass --yes to confirm "
            "(also required with --dry-run)",
            remote_facts,
        )

    # -- 3. precheck ----------------------------------------------------------
    precheck = remote.run_json(
        ["transfer-precheck", "--harness", harness, "--name", name]
    )
    conflicts = precheck.get("conflicts") or []
    if conflicts:
        raise TransferError(
            ipc_errors.TRANSFER_CONFLICT,
            f"target {remote_node} cannot take {name!r}: " + "; ".join(conflicts),
        )

    # -- 4. all infrastructure prerequisites, before any source mutation ----
    image = container_image or default_image()
    session_ref = spec.get("sessionRef")
    session_file = _preflight(
        host=host, harness=harness, remote=remote, docker=docker,
        containerized=containerized, image=image, home=home,
        with_credentials=with_credentials,
        cwd=str(spec.get("cwd") or ""), session_ref=session_ref,
    )
    cred_bundle: dict[str, Path] = {}
    if containerized and with_credentials:
        try:
            cred_bundle = credential_bundle(harness, home)
        except Exception as error:
            code = getattr(error, "code", ipc_errors.TRANSFER_CREDENTIALS)
            raise TransferError(code, str(error)) from error

    destination_cwd = _resolve_destination_cwd(target_cwd, spec.get("cwd"), remote)

    # A positive inspect of this exact tag avoids the expensive save/scp.
    # Probe BEFORE quiesce, including dry-run; transport errors must not
    # stop a healthy source just to discover the target is unreachable.
    target_has_image = containerized and remote.image_present(image)
    if target_has_image:
        emit(f"target already has {image}; skipping docker save and image upload")

    if dry_run:
        return {
            "ok": True,
            "dryRun": True,
            "remote": remote_facts,
            "harness": harness,
            "name": name,
            "from": plan["actor"],
            "toHost": host,
            "toNode": remote_node,
            "cwd": {"source": spec.get("cwd"), "target": destination_cwd},
            "sessionRef": session_ref,
            "sessionFile": str(session_file) if session_file else None,
            "sessionFileBytes": (
                session_file.stat().st_size if session_file else None
            ),
            "agent": plan.get("agent"),
            "unreadInbox": plan.get("unreadInbox"),
            **(
                {
                    "containerized": True,
                    "containerImage": image,
                    "imageTransferSkipped": target_has_image,
                    "pinnedOwner": plan["owner"],
                    "credentials": sorted(cred_bundle),
                }
                if containerized
                else {}
            ),
        }

    # -- 5. quiesce (point of no return for the SOURCE's running state) --------
    quiesced = local_request("transfer.quiesce", params)
    spec = dict(_required(quiesced.get("spec"), "spec"))
    session_ref = spec.get("sessionRef") or session_ref
    emit(f"quiesced on {plan['nodeId']} (sessionRef={session_ref})")
    try:
        # -- 6. ship -----------------------------------------------------------
        emit(f"rsync worktree {spec.get('cwd')} -> {host}:{destination_cwd}")
        remote.sync_tree(Path(str(spec["cwd"])), destination_cwd)
        if containerized:
            staged = remote.run_json(
                ["transfer-cred-stage", "--name", name, "--harness", harness]
            )
            staging_path = str(_required(staged.get("path"), "path"))
            if not target_has_image:
                emit(f"docker save {image} -> {host}:{staging_path}/image.tar")
                fd, tar_name = tempfile.mkstemp(
                    prefix="hyprial-worker-image-", suffix=".tar"
                )
                os.close(fd)
                tar_path = Path(tar_name)
                try:
                    docker.run(
                        ["save", "-o", str(tar_path), image], timeout=1800.0
                    )
                    emit(f"image tar {tar_path.stat().st_size} bytes -> {host}")
                    remote.upload(tar_path, f"{staging_path}/image.tar")
                finally:
                    tar_path.unlink(missing_ok=True)
            for arcname, local_file in cred_bundle.items():
                emit(f"credential {arcname} -> {host} (staging, 0600)")
                remote.upload(local_file, f"{staging_path}/{arcname}")
            remote.run_json(
                [
                    "transfer-cred-stage",
                    "--name",
                    name,
                    "--harness",
                    harness,
                    "--finalize",
                ]
            )
        if session_ref and session_file is not None:
            session_argv = _session_path_argv(
                harness, destination_cwd, str(session_ref), session_file, home
            )
            if containerized:
                # The transcript must land in the per-worker container home
                # (bind-mounted as the harness session dir), not the target
                # user's real HOME.
                container_home = remote.run_json(
                    [
                        "transfer-container-home",
                        "--name",
                        name,
                        "--harness",
                        harness,
                    ]
                )
                session_argv += [
                    "--home",
                    str(_required(container_home.get("home"), "home")),
                ]
            answer = remote.run_json(session_argv)
            target_path = str(_required(answer.get("path"), "path"))
            upload_file = session_file
            if harness == "pi":
                # pi refuses to resume a session whose header cwd is absent
                # on this machine (assertSessionCwdExists); repoint the
                # header at the target cwd.  The source file is untouched.
                fd, adapted_name = tempfile.mkstemp(
                    prefix="hyprial-transfer-", suffix=".jsonl"
                )
                os.close(fd)
                upload_file = rewrite_pi_session_cwd(
                    session_file, destination_cwd, Path(adapted_name)
                )
            emit(f"session file -> {target_path}")
            remote.upload(upload_file, target_path)
            if upload_file != session_file:
                upload_file.unlink(missing_ok=True)
            if harness == "claude":
                # claude's auto-memory is per-project sibling state
                # (``<session-dir>/memory/``); the session transcript
                # references it.  Migrate it when present — the live worker
                # resolves memory through the CURRENT project dir, so the
                # copy is functional, not cosmetic.
                memory_dir = session_file.parent / "memory"
                if memory_dir.is_dir():
                    remote_memory = str(Path(target_path).parent / "memory")
                    emit(f"claude memory dir -> {remote_memory}")
                    remote.sync_tree(memory_dir, remote_memory)
        # -- 7. receive (commit point; strict resume lives here) ---------------
        receive_spec = {**spec, "cwd": destination_cwd}
        if containerized:
            # Decision D-D: pin the SOURCE owner; the target daemon mints
            # the URI with it instead of its ambient environment owner.
            receive_spec["containerized"] = True
            receive_spec["pinnedOwner"] = plan["owner"]
            if container_image is not None:
                receive_spec["containerImage"] = image
        receive_payload = {
            "spec": receive_spec,
            "pins": list((quiesced.get("agent") or {}).get("pinnedAdapters") or []),
            "strictTimeoutSeconds": strict_timeout,
            "credentials": bool(with_credentials),
        }
        received = remote.run_json(
            ["transfer-receive"], stdin=json.dumps(receive_payload).encode()
        )
        new_actor = str(_required(received.get("actor"), "actor"))
        emit(f"received on {remote_node}: {new_actor} (resume verified)")
    except BaseException as error:
        # Roll the source back; the rollback result rides along so the
        # operator can see the worker is alive again (or why it is not).
        rollback: dict[str, Any] | None = None
        rollback_error: str | None = None
        try:
            rollback = local_request("transfer.resume", {"spec": spec})
        except Exception as rb_error:  # noqa: BLE001 - report, never mask
            rollback_error = str(rb_error)
        if isinstance(error, TransferError):
            code = error.code
        elif isinstance(error, RemoteError):
            code = "TRANSFER_REMOTE_FAILED"
        else:
            code = "TRANSFER_FAILED"
        raise TransferError(
            code,
            f"transfer of {harness}:{name} to {host} failed after quiesce; "
            f"the source was rolled back: {error}",
            {
                "rollback": rollback,
                "rollbackError": rollback_error,
                "stage": "ship/receive",
            },
        ) from error

    # -- 8. complete (source cleanup; failure here is LOUD but never rolls back)
    try:
        completed = local_request(
            "transfer.complete", {"name": name, "provider": harness}
        )
    except Exception as error:  # noqa: BLE001 - the worker is already live
        emit(
            f"warning: target has {name!r} live, but source cleanup failed: "
            f"{error}; run 'hyprial agent destroy {name}' on the source manually"
        )
        completed = {"ok": False, "error": str(error)}
    return {
        "ok": True,
        "harness": harness,
        "name": name,
        "from": old_actor,
        "to": new_actor,
        "remote": remote_facts,
        "toHost": host,
        "toNode": remote_node,
        "cwd": destination_cwd,
        "sessionRef": session_ref,
        "resumeVerified": received.get("sessionRef") == session_ref,
        "source": completed,
        **(
            {
                "containerized": True,
                "containerImage": image,
                "imageTransferSkipped": target_has_image,
                "pinnedOwner": plan["owner"],
                # Decision D-D: the old/new URIs are spelled out so the
                # machine-segment change is never a silent rename.
                "uriChange": {"from": old_actor, "to": new_actor},
            }
            if containerized
            else {}
        ),
    }


def _session_path_argv(
    harness: str, cwd: str, session_ref: str, source: Path, home: Path
) -> list[str]:
    """Argv for the remote path resolver; codex needs the relative layout."""

    argv = [
        "transfer-session-path",
        "--harness",
        harness,
        "--cwd",
        cwd,
        "--ref",
        session_ref,
        "--filename",
        source.name,
    ]
    if harness == "codex":
        sessions_root = home / ".codex" / "sessions"
        try:
            relative = source.relative_to(sessions_root)
        except ValueError as error:
            raise TransferError(
                "TRANSFER_SESSION_FILE",
                f"codex rollout {source} is not under {sessions_root}",
            ) from error
        argv += ["--sessions-rel", str(relative)]
    return argv
