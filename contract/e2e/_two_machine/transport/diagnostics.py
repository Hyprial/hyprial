from __future__ import annotations

from pathlib import Path

def _flush_ci_presence_trace(ports, ) -> None:
    """Persist only the sanitized ring on a CI PASS without copying logs."""

    if not ports._ci_capture_enabled():
        return
    try:
        root = ports.artifacts_root()
        root.mkdir(parents=True, exist_ok=True)
        ports.PRESENCE_TRACE.write(root / f"{ports.PREFIX}-presence-trace.json")
    except Exception as error:  # noqa: BLE001 - diagnostics never replace scenario result
        print(f"RESULT info E2E-001 presence trace lost ({type(error).__name__})")

def _capture_docker_state(ports, label: str) -> None:
    """One bounded A/B/C state snapshot; diagnostics never decide the scene."""

    states: dict[str, object] = {}
    for node in (*ports.NODES, ports.NODE_C):
        try:
            state = ports.json.loads(
                ports.docker(["inspect", "--format", "{{json .State}}", node], timeout=3.0)
            )
            if not isinstance(state, dict):
                raise TypeError("Docker State was not an object")
            states[node] = {
                "running": state.get("Running") if type(state.get("Running")) is bool else None,
                "status": state.get("Status") if isinstance(state.get("Status"), str) else None,
                "exitCode": state.get("ExitCode") if type(state.get("ExitCode")) is int else None,
                "startedAt": state.get("StartedAt") if isinstance(state.get("StartedAt"), str) else None,
                "finishedAt": state.get("FinishedAt") if isinstance(state.get("FinishedAt"), str) else None,
            }
        except Exception as error:  # noqa: BLE001 - diagnostics never replace scenario failure
            states[node] = {"errorType": type(error).__name__}
    try:
        ports.PRESENCE_TRACE.record_docker_state(label, states)
    except Exception as error:  # noqa: BLE001 - diagnostics never replace scenario failure
        print(f"RESULT info E2E-001 Docker state trace lost ({type(error).__name__})")

def capture_failure(ports, label: str = "failure") -> Path:
    """Dump container logs and daemon evidence to a kept directory.

    Uses `docker cp` because it also works on stopped containers (a failure
    may occur mid offline-queue phase).  Containers are still removed by the
    cleanup; only the evidence directory is retained.

    `--keep-evidence` runs this on a PASS too.  The `msg/status` diagnostics
    live half on the querying node and half on the answering one -- a node
    holding no records replies with nothing at all, so only its own log can
    say whether it was ever asked -- and that half is unreachable once the
    containers are reaped.
    """

    stamp = ports.datetime.now(ports.UTC).strftime("%Y%m%dT%H%M%SZ")
    # #131⑤: under CI (HYPRIAL_E2E_ROOT / HYPRIAL_E2E_ARTIFACTS set) evidence must
    # land in the run-scoped artifact store — the one the collect step
    # inventories and the success()-only cleanup preserves on failure.  The
    # hardcoded /tmp channel was invisible to collection, so the gate read
    # "no E2E failure artifacts preserved" while the scene sat in /tmp all
    # along.  Standalone runs keep the legacy /tmp channel (host-persistent,
    # age-swept by #69, never touched by CI cleanup).
    if ports.os.environ.get("HYPRIAL_E2E_ARTIFACTS") or ports.os.environ.get("HYPRIAL_E2E_ROOT"):
        failure_dir = ports.artifacts_root() / f"{ports.PREFIX}-{label}-{stamp}"
    else:
        failure_dir = ports.Path("/tmp") / f"{ports.PREFIX}-{label}-{stamp}"
    failure_dir.mkdir(parents=True, exist_ok=True)
    for node in ports.ALL_NODES:
        try:
            logs = ports.sh(["docker", "logs", node], timeout=30.0, check=False)
            (failure_dir / f"{node}.log").write_text(
                logs.stdout + logs.stderr, encoding="utf-8"
            )
        except OSError:
            pass
        for container_path, name in (
            ("/data/state/logs/daemon.jsonl", f"{node}.daemon.jsonl"),
            ("/data/state/desired-state.json.v1", f"{node}.desired-state.json"),
        ):
            try:
                ports.sh(
                    ["docker", "cp", f"{node}:{container_path}", str(failure_dir / name)],
                    timeout=30.0,
                    check=False,
                )
            except (ports.DockerError, OSError):
                pass
    try:
        ports.PRESENCE_TRACE.write(failure_dir / "presence-trace.json")
    except Exception as error:  # noqa: BLE001 - trace loss does not rewrite the scenario verdict
        print(f"RESULT info E2E-001 presence trace lost ({type(error).__name__})")
    return failure_dir


def delivery_status(ports, node: str, sender_actor: str, message_id: str, *, timeout_seconds: float = 5.0) -> dict[str, object]:
    """Ask `node` what became of one message `sender_actor` sent."""
    return ports.exec_json(
        node, "message", "status", "--from", sender_actor,
        "--message-id", message_id, "--timeout", str(timeout_seconds), timeout=60.0,
    )


def diagnostics_of(ports, label: str, value: dict[str, object]) -> dict[str, object]:
    """The `how was this set assembled` block that accompanies a verdict."""
    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, dict):
        raise AssertionError(f"{label}: message status carries no diagnostics block: {value}")
    return diagnostics


def format_diagnostics(ports, value: dict[str, object]) -> str:
    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return "diagnostics=absent"
    return (
        f"localRecords={diagnostics.get('localRecords')} "
        f"meshReplies={diagnostics.get('meshReplies')} "
        f"meshHolders={diagnostics.get('meshHolders')} "
        f"undecodable={diagnostics.get('undecodableReplies')} "
        f"replyErrors={diagnostics.get('replyErrors')} "
        f"conflicts={diagnostics.get('conflictingMessageIds')} "
        f"noKnownLoss={diagnostics.get('noKnownLoss')}"
    )


def assert_no_known_loss(ports, label: str, value: dict[str, object]) -> None:
    """Reject a verdict merged from a set that lost replies."""
    diagnostics = diagnostics_of(ports, label, value)
    if diagnostics.get("noKnownLoss") is not True:
        raise AssertionError(
            f"{label}: verdict was merged from a set that lost replies: {diagnostics}"
        )
