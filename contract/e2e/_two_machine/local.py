from __future__ import annotations

from _daemon_runner import IsolatedDaemon
from _runner_shared import org as _org

def pending_ids_local(ports, value: dict[str, object]) -> list[object]:
    pending = value.get("pendingMessages", [])
    return (
        [item.get("messageId") for item in pending if isinstance(item, dict)]
        if isinstance(pending, list)
        else []
    )

def wait_for_target_local(ports, daemon: IsolatedDaemon, actor: str, *, absent: bool = False) -> None:
    daemon.wait(
        # Node presence lives in `host list`; targets lists delivery promises.
        lambda: daemon.command("host", "list", "--json"),
        lambda value: absent
        is not any(
            item.get("nodeId") == actor
            for item in value.get("hosts", [])
            if isinstance(item, dict)
        ),
    )

def roundtrip_local(ports, 
    phase: str,
    sender: IsolatedDaemon,
    sender_actor: str,
    receiver: IsolatedDaemon,
    receiver_actor: str,
    text: str,
) -> None:
    outbound = sender.command(
        "message", "send", "--from", sender_actor, "--to", receiver_actor,
        "--summary", text, text, "--json"
    )
    message_id = outbound["messageId"]
    receiver.wait(
        lambda: receiver.ps(),
        lambda value: ports.pending_ids_local(value) == [message_id],
    )
    ack = receiver.command("message", "ack", "--from", receiver_actor, message_id, "--json")
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"{phase} receiver acknowledgement failed: {ack}")
    reply = receiver.command(
        "message",
        "send",
        "--from",
        receiver_actor,
        "--to",
        sender_actor,
        "--summary",
        f"{text}:reply",
        f"{text}:reply",
        "--json",
    )
    reply_id = reply["messageId"]
    sender.wait(
        lambda: sender.ps(),
        lambda value: ports.pending_ids_local(value) == [reply_id],
    )
    ack = sender.command("message", "ack", "--from", sender_actor, reply_id, "--json")
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"{phase} sender acknowledgement failed: {ack}")
    print(f"RESULT ok {phase} roundtrip {text}")

def run_local(ports, ) -> None:
    executable = ports.hyprial_executable()
    # Keep Unix-socket paths well under the 104-byte sun_path limit: the
    # default macOS temp root resolves /var/folders -> /private/var/folders,
    # which alone can push `<root>/<node>/state/daemon.sock` past it.
    short_base = ports.Path("/tmp")
    with ports.tempfile.TemporaryDirectory(
        prefix="hyprial-e2e001-", dir=short_base
    ) as temporary:
        root = ports.Path(temporary)
        host_a = ports.free_tcp_endpoint()
        host_b = ports.free_tcp_endpoint()
        left = ports.IsolatedDaemon(executable, root / "machine-a", "machine-a")
        right = ports.IsolatedDaemon(executable, root / "machine-b", "machine-b")
        try:
            left.start(init_args=("--listen", host_a, "--connect", host_b))
            right.start(init_args=("--listen", host_b, "--connect", host_a))

            # resolve-or-reject (Allen b): registered personas, never bare nodes.
            left.command("agent", "create", "--name", ports.DRIVER, "--json")
            right.command("agent", "create", "--name", ports.DRIVER, "--json")

            # Hard cut (send links, 2026-10-15): agent-to-agent sends need a
            # shared writable org space, or every first send holds forever.
            org_space = _org.ensure_local_org(
                left, ports.driver_uri_local(left), prefix="e2e001-local"
            )
            _org.invite_owner(left, org_space, _org.owner_of(right))
            _org.join_org(
                right, org_space, actor=ports.driver_uri_local(right)
            )

            ports.assert_endpoints("phase1/machine-a", left.ps(), host_a, host_b)
            ports.assert_endpoints("phase1/machine-b", right.ps(), host_b, host_a)

            ports.wait_for_target_local(left, "machine-b")
            ports.wait_for_target_local(right, "machine-a")
            ports.roundtrip_local("phase1", left, ports.driver_uri_local(left), right, ports.driver_uri_local(right), "CONTRACT_E2E_REQUEST_1")

            left.stop()
            right.stop()
            left.start()
            right.start()

            ports.assert_endpoints("phase2/machine-a", left.ps(), host_a, host_b)
            ports.assert_endpoints("phase2/machine-b", right.ps(), host_b, host_a)

            ports.wait_for_target_local(left, "machine-b")
            ports.wait_for_target_local(right, "machine-a")
            ports.roundtrip_local("phase2", right, ports.driver_uri_local(right), left, ports.driver_uri_local(left), "CONTRACT_E2E_REQUEST_2")
        finally:
            right.stop()
            left.stop()

def driver_uri_local(ports, daemon) -> str:  # IsolatedDaemon, local mode
    info = daemon.ps().get("daemon", {})
    owner = str(info.get("owner") or "").strip() if isinstance(info, dict) else ""
    node = str(info.get("nodeId") or "").strip() if isinstance(info, dict) else ""
    if not owner or not node:
        raise AssertionError(f"daemon did not report a usable identity: {info}")
    agents = daemon.command("agent", "list", "--json").get("agents", [])
    record = next((item for item in agents if item.get("actor") == ports.DRIVER), None)
    uri = record.get("uri") if isinstance(record, dict) else None
    if not isinstance(uri, str) or not uri.startswith(f"agent:{owner}:{node}:a."):
        raise AssertionError(f"driver did not publish an id address: {record}")
    return uri
