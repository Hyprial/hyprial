#!/usr/bin/env python3
"""E2E-028: a fixed Tailcat slot survives a remote-path drop without redial.

This negative control builds both real Tailcat executables from this checkout,
starts two disposable daemons over the loopback labrelay, and establishes an
OrgFS-backed session.  It then terminates the dialling node's owned Tailcat
sidecar, which drops that node's slot path to the remote peer.  The supervisor
must restore the sidecar and the same fixed slot without changing the peer's
OrgFS directory address or invoking the Zenoh transport reconfigure path.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    scenario_tempdir,
    short_temp_root,
)

SCENARIO = "E2E-028"
DRIVER = "remote-drop-driver"
ORG = "group-remote-drop"
OVERALL_TIMEOUT_SECONDS = 300
BUILD_TIMEOUT_SECONDS = 180
WAIT_SECONDS = 60
ROOT = Path(__file__).resolve().parents[2]
TAILCAT_SOURCE = ROOT / "sidecar" / "hyprial-tailcat"

_TAILCAT = runpy.run_path(
    str(Path(__file__).with_name("run-tailcat-org-invite-join.py"))
)


class ScenarioTimeout(AssertionError):
    """The runner's outer deadline fired."""


def _emit(**event: object) -> None:
    print(json.dumps({"v": 1, **event}, separators=(",", ":")), flush=True)


def _wait(
    description: str,
    probe: Callable[[], Any],
    accept: Callable[[Any], bool],
    *,
    timeout: float,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (AssertionError, OSError, subprocess.SubprocessError) as error:
            last = f"{type(error).__name__}: {error}"
        if accept(last):
            return last
        time.sleep(0.25)
    raise AssertionError(
        f"{description} did not complete within {timeout:g}s; last={last!r}"
    )


def _build(source: Path, package: str, output: Path) -> None:
    environment = dict(os.environ)
    environment["CGO_ENABLED"] = "0"
    environment.setdefault("GOTOOLCHAIN", "auto")
    result = subprocess.run(
        [
            "go",
            "build",
            "-mod=readonly",
            "-trimpath",
            "-o",
            str(output),
            package,
        ],
        cwd=source,
        env=environment,
        text=True,
        capture_output=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise AssertionError(
            f"checkout sidecar build failed for {package} rc={result.returncode}: {detail}"
        )
    if not output.is_file() or not os.access(output, os.X_OK):
        raise AssertionError(f"checkout sidecar build produced no executable: {output}")


def _build_sidecars(root: Path) -> tuple[Path, Path, str]:
    output = root / "tailcat-bin"
    output.mkdir(parents=True)
    tailcat = output / "hyprial-tailcat"
    labrelay = output / "hyprial-tailcat-labrelay"
    _build(TAILCAT_SOURCE, "./cmd/hyprial-tailcat", tailcat)
    _build(TAILCAT_SOURCE, "./cmd/hyprial-tailcat-labrelay", labrelay)
    digest = hashlib.sha256(tailcat.read_bytes()).hexdigest()
    return tailcat, labrelay, digest


def _events(daemon: IsolatedDaemon, name: str | None = None) -> list[dict[str, Any]]:
    path = daemon.root / "state" / "logs" / "daemon.jsonl"
    if not path.is_file():
        return []
    found: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (name is None or value.get("event") == name):
            found.append(value)
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _forwarding_status(daemon: IsolatedDaemon) -> dict[str, Any]:
    value = daemon.ps()
    forwarding = value.get("forwarding")
    if not isinstance(forwarding, dict):
        raise AssertionError(f"{daemon.node_id} omitted forwarding status: {value}")
    return forwarding


def _sidecar_pid(daemon: IsolatedDaemon) -> int | None:
    pid = _forwarding_status(daemon).get("pid")
    return pid if isinstance(pid, int) and pid > 1 else None


def _address(daemon: IsolatedDaemon) -> str | None:
    value = _TAILCAT["_read_address"](daemon)
    return value if isinstance(value, str) and value else None


def _directory_rows(daemon: IsolatedDaemon) -> list[dict[str, Any]]:
    value = daemon.command("org", "network", ORG, "--json", timeout=20)
    directory = value.get("directory")
    rows = directory.get(ORG) if isinstance(directory, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise AssertionError(f"org network returned no directory rows: {value}")
    return rows


def _directory_address(
    daemon: IsolatedDaemon,
    space_id: str,
    *,
    owner: str,
    device_id: str,
) -> str | None:
    from hyprial.identity.impl.org_directory.models import directory_device_path

    value = daemon.command(
        "org", "fs",
        "read",
        space_id,
        directory_device_path(owner, device_id),
        "--json",
        timeout=20,
    )
    text = value.get("text")
    if not isinstance(text, str):
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    address = record.get("address") if isinstance(record, dict) else None
    return address if isinstance(address, str) and address else None


def _owner(daemon: IsolatedDaemon) -> str:
    value = daemon.ps()
    owner = value.get("daemon", {}).get("owner")
    if not isinstance(owner, str) or not owner:
        raise AssertionError(f"{daemon.node_id} omitted its owner: {value}")
    return owner


def _pending_ids(daemon: IsolatedDaemon) -> set[str]:
    value = daemon.ps()
    return {
        str(item["messageId"])
        for item in value.get("pendingMessages", [])
        if isinstance(item, dict) and isinstance(item.get("messageId"), str)
    }


def _send_and_ack(
    sender: IsolatedDaemon,
    receiver: IsolatedDaemon,
    marker: str,
) -> str:
    agents = receiver.command("agent", "list", "--json").get("agents", [])
    record = next((item for item in agents if item.get("actor") == DRIVER), None)
    target = record.get("uri") if isinstance(record, dict) else None
    if not isinstance(target, str) or not target.startswith("agent:") or ":a." not in target:
        raise AssertionError(f"receiver omitted the driver's official id address: {record}")
    sent = sender.command(
        "message",
        "send",
        "--from",
        DRIVER,
        "--to",
        target,
        "--summary",
        marker,
        marker,
        "--json",
        timeout=20,
    )
    message_id = sent.get("messageId")
    if not isinstance(message_id, str):
        raise AssertionError(f"send returned no messageId: {sent}")
    _wait(
        f"{marker} delivery",
        lambda: _pending_ids(receiver),
        lambda pending: message_id in pending,
        timeout=WAIT_SECONDS,
    )
    acknowledged = receiver.command(
        "message", "ack", "--from", DRIVER, message_id, "--json", timeout=20
    )
    if acknowledged.get("acknowledged") is not True:
        raise AssertionError(f"message acknowledgement failed: {acknowledged}")
    return message_id


def _active_edge(
    daemon_a: IsolatedDaemon,
    daemon_b: IsolatedDaemon,
) -> tuple[IsolatedDaemon, IsolatedDaemon, dict[str, Any]] | None:
    for dialer, peer in ((daemon_a, daemon_b), (daemon_b, daemon_a)):
        forwarding = _forwarding_status(dialer)
        peers = forwarding.get("peers")
        assigned = forwarding.get("slots", {}).get("assigned")
        if (
            forwarding.get("state") == "running"
            and isinstance(peers, list)
            and peers
            and isinstance(assigned, list)
            and assigned
            and all(item.get("dialed") is True for item in peers if isinstance(item, dict))
        ):
            return dialer, peer, forwarding
    return None


def _event_delta(
    daemon: IsolatedDaemon,
    baselines: dict[str, int],
    name: str,
) -> list[dict[str, Any]]:
    return _events(daemon, name)[baselines[name] :]


def _run(root: Path) -> int:
    tailcat, labrelay_binary, binary_sha256 = _build_sidecars(root)
    labrelay, derp = _TAILCAT["_start_labrelay"](str(labrelay_binary))
    a_listen = free_tcp_endpoint()
    b_listen = free_tcp_endpoint()
    daemon_a = IsolatedDaemon(
        hyprial_executable(),
        root / "a",
        "remote-drop-a",
        listen=a_listen,
        tailcat_forwarding=True,
    )
    daemon_b = IsolatedDaemon(
        hyprial_executable(),
        root / "b",
        "remote-drop-b",
        listen=b_listen,
        tailcat_forwarding=True,
    )
    _TAILCAT["_configure_tailcat"](
        daemon_a,
        owner="remote-drop-alice",
        listen=a_listen,
        binary=str(tailcat),
        derp=derp,
    )
    _TAILCAT["_configure_tailcat"](
        daemon_b,
        owner="remote-drop-bob",
        listen=b_listen,
        binary=str(tailcat),
        derp=derp,
    )
    cleanup_errors: list[str] = []
    try:
        daemon_a.start()
        daemon_b.start()
        for daemon in (daemon_a, daemon_b):
            _wait(
                f"{daemon.node_id} sidecar address",
                lambda current=daemon: _address(current),
                lambda value: isinstance(value, str),
                timeout=WAIT_SECONDS,
            )
            daemon.command("agent", "create", "--name", DRIVER, "--json")

        created = daemon_a.command("org", "create", ORG, "--json", timeout=30)
        if created.get("ok") is not True or created.get("devicePublished") is not True:
            raise AssertionError(f"org create did not publish the device: {created}")
        space_id = created.get("spaceId")
        if not isinstance(space_id, str) or not space_id:
            raise AssertionError(f"org create returned no spaceId: {created}")
        invited = daemon_a.command(
            "org", "invite", ORG, "--user", "remote-drop-bob", "--json", timeout=30
        )
        link = invited.get("link")
        if not isinstance(link, str) or not link.startswith("https://"):
            raise AssertionError(f"org invite returned no https link: {invited}")
        joined = daemon_b.command(
            "org", "join", "-", "--json", stdin=link, timeout=120
        )
        if joined.get("ok") is not True or joined.get("org") != ORG:
            raise AssertionError(f"org join failed: {joined}")

        for daemon in (daemon_a, daemon_b):
            _wait(
                f"{daemon.node_id} directory convergence",
                lambda current=daemon: sorted(
                    str(row.get("deviceId")) for row in _directory_rows(current)
                ),
                lambda devices: devices
                == [daemon_a.node_id, daemon_b.node_id],
                timeout=WAIT_SECONDS,
            )
        dialer, peer, forwarding_before = _wait(
            "one dialer to report an active fixed-slot edge",
            lambda: _active_edge(daemon_a, daemon_b),
            lambda value: isinstance(value, tuple),
            timeout=WAIT_SECONDS,
        )
        _send_and_ack(dialer, peer, "before-remote-slot-drop")

        peer_owner = _owner(peer)
        peer_address_before = _wait(
            "dialer's directory to contain the peer's published address",
            lambda: _directory_address(
                dialer,
                space_id,
                owner=peer_owner,
                device_id=peer.node_id,
            ),
            lambda address: address == _address(peer),
            timeout=WAIT_SECONDS,
        )
        endpoints_before = forwarding_before.get("endpoints")
        connect_before = daemon_a.ps()
        if dialer is daemon_b:
            connect_before = daemon_b.ps()
        connect_before = connect_before.get("zenoh", {}).get("connect")
        if not isinstance(endpoints_before, list) or not endpoints_before:
            raise AssertionError(f"fixed slot endpoints are absent: {forwarding_before}")
        if not isinstance(connect_before, list) or not set(endpoints_before) <= set(
            connect_before
        ):
            raise AssertionError(
                f"fixed slots are not in the Zenoh connect set: {connect_before}"
            )

        event_names = (
            "zenoh.forwarding.redialed",
            "zenoh.forwarding.changed",
            "zenoh.forwarding.redial_failed",
            "zenoh.session.rebuilt",
            "zenoh.session.closed",
        )
        baselines = {name: len(_events(dialer, name)) for name in event_names}
        old_sidecar_pid = _sidecar_pid(dialer)
        if old_sidecar_pid is None:
            raise AssertionError(f"dialer has no owned sidecar pid: {forwarding_before}")
        os.kill(old_sidecar_pid, signal.SIGTERM)
        _wait(
            "dropped sidecar process to exit",
            lambda: _alive(old_sidecar_pid),
            lambda alive: alive is False,
            timeout=15,
        )
        restarted = _wait(
            "sidecar supervisor to restore the remote slot",
            lambda: _forwarding_status(dialer),
            lambda value: (
                isinstance(value.get("pid"), int)
                and value["pid"] != old_sidecar_pid
                and value.get("state") == "running"
                and value.get("endpoints") == endpoints_before
                and value.get("restartRequired") is False
                and isinstance(value.get("peers"), list)
                and bool(value["peers"])
            ),
            timeout=WAIT_SECONDS,
        )
        _wait(
            "peer directory address to remain unchanged",
            lambda: _directory_address(
                dialer,
                space_id,
                owner=peer_owner,
                device_id=peer.node_id,
            ),
            lambda address: address == peer_address_before,
            timeout=WAIT_SECONDS,
        )
        if _address(peer) != peer_address_before:
            raise AssertionError("peer sidecar address changed during the slot outage")

        delivered = _send_and_ack(dialer, peer, "after-remote-slot-return")
        time.sleep(2.0)
        redialed = _event_delta(
            dialer, baselines, "zenoh.forwarding.redialed"
        )
        changed = _event_delta(dialer, baselines, "zenoh.forwarding.changed")
        redial_failed = _event_delta(
            dialer, baselines, "zenoh.forwarding.redial_failed"
        )
        rebuilt = _event_delta(dialer, baselines, "zenoh.session.rebuilt")
        reconfigure_closed = [
            event
            for event in _event_delta(
                dialer, baselines, "zenoh.session.closed"
            )
            if event.get("reason") == "reconfigure"
        ]
        if redialed != [] or changed != []:
            raise AssertionError(
                f"remote slot return changed the forwarding set: "
                f"redialed={redialed} changed={changed}"
            )
        if redial_failed != [] or rebuilt != [] or reconfigure_closed != []:
            raise AssertionError(
                "remote slot return invoked the transport reconfigure path: "
                f"redialFailed={redial_failed} rebuilt={rebuilt} "
                f"closed={reconfigure_closed}"
            )
        if restarted.get("endpoints") != endpoints_before:
            raise AssertionError(
                f"fixed slot endpoints changed: {endpoints_before} -> "
                f"{restarted.get('endpoints')}"
            )
        _emit(
            event="remote-drop-no-rebuild",
            source=str(TAILCAT_SOURCE.relative_to(ROOT)),
            binarySha256=binary_sha256,
            dialer=dialer.node_id,
            peer=peer.node_id,
            oldSidecarPid=old_sidecar_pid,
            newSidecarPid=restarted.get("pid"),
            peerAddressUnchanged=True,
            endpointsUnchanged=True,
            redialed=[],
            changed=[],
            reconfigureClosed=[],
            rebuilt=[],
            deliveredMessageId=delivered,
        )
    finally:
        for daemon in (daemon_b, daemon_a):
            try:
                daemon.stop()
            except BaseException as error:  # noqa: BLE001 - attempt both owned daemons
                cleanup_errors.append(
                    f"{daemon.node_id}: {type(error).__name__}: {error}"
                )
        try:
            _TAILCAT["_stop_labrelay"](labrelay)
        except BaseException as error:  # noqa: BLE001 - preserve cleanup verdict
            cleanup_errors.append(f"labrelay: {type(error).__name__}: {error}")
    if cleanup_errors:
        raise AssertionError(f"owned process cleanup failed: {cleanup_errors}")
    print(
        "RESULT PASS E2E-028 remote-drop-no-rebuild "
        "redialed=0 changed=0 reconfigure=0 message=delivered cleanup=clean"
    )
    return 0


def main() -> int:
    def expired(_signum: int, _frame: object) -> None:
        raise ScenarioTimeout(
            f"{SCENARIO} exceeded its {OVERALL_TIMEOUT_SECONDS}s overall deadline"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(OVERALL_TIMEOUT_SECONDS)
    try:
        with scenario_tempdir(SCENARIO.lower(), directory=short_temp_root()) as root:
            return _run(root)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssertionError,
        OSError,
        KeyError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        print(f"RESULT FAIL E2E-028 remote-drop-no-rebuild {error}")
        raise SystemExit(1) from error
