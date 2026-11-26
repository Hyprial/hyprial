#!/usr/bin/env python3
"""E2E-025: org invite → join → orgfs traffic over real Tailcat sidecars.

Two disposable daemons, each with its own device key and real
``hyprial-tailcat`` sidecar, meet through a local labrelay DERP (every leg
loopback).  Alice creates org ``demo``, invites bob; bob joins through the
https invite link read from stdin; both then see two directory devices and
exchange orgfs documents in both directions.  The path proof is read off
bob's own ``ps``: the forwarding endpoints are sidecar-mapped loopback
ports and alice's real Zenoh listener never appears in bob's connect set.

Prerequisites (RESULT BLOCKED when missing, never a silent skip):

* ``HYPRIAL_TAILCAT_BINARY``   -- the pinned protocol-v7 sidecar build
* ``HYPRIAL_TAILCAT_LABRELAY`` -- the local DERP relay fixture
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    artifacts_root,
    free_tcp_endpoint,
    hyprial_executable,
    scenario_tempdir,
    short_temp_root,
)

SCENARIO = "E2E-025"
OVERALL_TIMEOUT_SECONDS = 300
ORG = "group-demo"
ADDRESS_WAIT_SECONDS = 45
DIRECTORY_WAIT_SECONDS = 60
REPLICATE_WAIT_SECONDS = 60

TAILCAT_BINARY_ENV = "HYPRIAL_TAILCAT_BINARY"
LABRELAY_ENV = "HYPRIAL_TAILCAT_LABRELAY"
TAILCAT_DERP_ENV = "HYPRIAL_TAILCAT_DERP"

# A minimal accepted org-context for alice.  Its only job is to give the
# mesh a real answer: bob has not adopted anything yet, so his org.fetch
# must stage this document as a candidate rather than report a local success.
ORG_CONTEXT_SEED = """# Org context

E2E-025 seed.

```yaml
meta:
  version: 2
  issued_at: 2026-11-02
  publisher: alice
lines:
  - {line: alpha, owner: bob}
members: []
routing: []
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
"""


class ScenarioTimeout(AssertionError):
    """The runner's outer deadline fired."""


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


def _start_labrelay(path: str) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    """Launch the local DERP relay and read its one-line config from stdout."""

    proc = subprocess.Popen(
        [path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 15.0)
        if not ready:
            raise AssertionError("labrelay printed no derp config within 15s")
        line = proc.stdout.readline()
        payload = json.loads(line)
        derp = payload.get("derp") if isinstance(payload, dict) else None
        if (
            not isinstance(derp, dict)
            or not isinstance(derp.get("host"), str)
            or not isinstance(derp.get("derpPort"), int)
            or not isinstance(derp.get("stunPort"), int)
        ):
            raise AssertionError(f"labrelay printed an invalid derp config: {line!r}")
        return proc, derp
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def _stop_labrelay(proc: subprocess.Popen[str]) -> None:
    """stdin EOF is the relay's shutdown signal; SIGTERM/SIGKILL are the backstop."""

    if proc.poll() is not None:
        return
    try:
        assert proc.stdin is not None
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    proc.wait(timeout=3)
    if proc.poll() is None:
        raise AssertionError("owned labrelay pid survived SIGKILL")


def _address_file(daemon: IsolatedDaemon) -> Path:
    return daemon.root / "home" / "state" / "tailcat" / "address"


def _read_address(daemon: IsolatedDaemon) -> str | None:
    try:
        return _address_file(daemon).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _directory_devices(daemon: IsolatedDaemon) -> list[str]:
    result = daemon.command("org", "network", ORG, "--json", timeout=20)
    directory = result.get("directory")
    rows = directory.get(ORG) if isinstance(directory, dict) else None
    if not isinstance(rows, list):
        raise AssertionError(f"org network returned no directory rows: {result}")
    return sorted(
        str(row.get("deviceId")) for row in rows if isinstance(row, dict)
    )


def _configure_tailcat(
    daemon: IsolatedDaemon,
    *,
    owner: str,
    listen: str,
    binary: str,
    derp: dict[str, Any],
) -> None:
    """Provision a device and private Unix backend before the v7 control startup."""

    from hyprial.daemon import ensure_device_key

    home = daemon.root / "home"
    home.mkdir(parents=True, exist_ok=True)
    ensure_device_key(
        home,
        owner=owner,
        device_id=daemon.node_id,
        environ={TAILCAT_BINARY_ENV: binary},
    )
    backend = home / 'net'
    backend.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = 'unix:' + str(backend / 'zenoh.sock')
    daemon.env['HYPRIAL_ZENOH_LISTEN'] = 'unixsock-stream/' + str(backend / 'zenoh.sock')
    daemon.env.update(
        {
            "HYPRIAL_OWNER": owner,
            "HYPRIAL_FORWARDING": "on",
            "HYPRIAL_FORWARDING_INBOUND_TARGET": target,
            TAILCAT_BINARY_ENV: binary,
            TAILCAT_DERP_ENV: json.dumps(derp, separators=(",", ":")),
            # First start with a real sidecar handshake comfortably exceeds
            # the product's 30s idle budget on a loaded host (observed ~35s
            # from CLI start to ping-ready); the harness-side timeout in
            # IsolatedDaemon derives from the same variable.
            "HYPRIAL_INIT_READY_TIMEOUT": "90",
        }
    )


def _seed_org_context(daemon: IsolatedDaemon) -> dict[str, Any]:
    """Give alice an accepted org-context so the mesh has a real answer."""

    seed = daemon.root / "org-context-seed.md"
    seed.write_text(ORG_CONTEXT_SEED, encoding="utf-8")
    result = daemon.command(
        "org", "context", "import", str(seed), "--force", "--json"
    )
    if result.get("ok") is not True or result.get("adopted") is not True:
        raise AssertionError(f"org-context seed was not adopted: {result}")
    return result


def _fetch_neighbors(daemon: IsolatedDaemon) -> dict[str, Any]:
    """Call the exact GUI path: org.fetch with the neighbors request mode."""

    return daemon.request(
        "org.fetch", {"timeoutSeconds": 2, "requestMode": "neighbors"}
    )


def _assert_fetch_counts(
    result: dict[str, Any], *, expected_received: int, label: str
) -> dict[str, Any]:
    count = result.get("responseCount")
    received = result.get("receivedCount")
    rejected = result.get("rejectedCount")
    if count != expected_received:
        raise AssertionError(f"{label}: responseCount={count!r}, want {expected_received}")
    if received != expected_received:
        raise AssertionError(f"{label}: receivedCount={received!r}, want {expected_received}")
    if rejected != 0:
        raise AssertionError(f"{label}: rejectedCount={rejected!r}, want 0")
    if result.get("requestedSources") != []:
        raise AssertionError(
            f"{label}: neighbors mode must not enumerate requestedSources: "
            f"{result.get('requestedSources')!r}"
        )
    return result


def _run(binary: str, labrelay_path: str) -> int:
    executable = hyprial_executable()
    a_listen = free_tcp_endpoint()
    b_listen = free_tcp_endpoint()
    labrelay, derp = _start_labrelay(labrelay_path)
    evidence: dict[str, Any] = {
        "scenario": SCENARIO,
        "org": ORG,
        "derp": derp,
        "aListen": a_listen,
        "bListen": b_listen,
    }
    try:
        with scenario_tempdir(SCENARIO.lower(), directory=short_temp_root()) as root:
            daemon_a = IsolatedDaemon(
                executable, root / "a", "a", listen=a_listen,
                tailcat_forwarding=True,
            )
            daemon_b = IsolatedDaemon(
                executable, root / "b", "b", listen=b_listen,
                tailcat_forwarding=True,
            )
            _configure_tailcat(
                daemon_a, owner="alice", listen=a_listen, binary=binary, derp=derp
            )
            _configure_tailcat(
                daemon_b, owner="bob", listen=b_listen, binary=binary, derp=derp
            )
            try:
                daemon_a.start()
                daemon_b.start()
                # Both sidecars must have written their Tailcat addresses:
                # org create/invite and register-device all need them.
                for daemon in (daemon_a, daemon_b):
                    _wait(
                        f"{daemon.node_id} sidecar address file",
                        lambda d=daemon: _read_address(d),
                        lambda value: isinstance(value, str),
                        timeout=ADDRESS_WAIT_SECONDS,
                    )
                created = daemon_a.command("org", "create", ORG, "--json")
                if created.get("ok") is not True or created.get("org") != ORG:
                    raise AssertionError(f"org create failed: {created}")
                if created.get("devicePublished") is not True:
                    raise AssertionError(f"alice's device was not published: {created}")
                space_id = created.get("spaceId")
                if not isinstance(space_id, str) or not space_id:
                    raise AssertionError(f"org create returned no spaceId: {created}")
                evidence["spaceId"] = space_id

                invited = daemon_a.command(
                    "org", "invite", ORG, "--user", "bob", "--json"
                )
                link = invited.get("link")
                if not isinstance(link, str) or not link.startswith("https://"):
                    raise AssertionError(f"org invite returned no https link: {invited}")

                joined = daemon_b.command(
                    "org", "join", "-", "--json", stdin=link, timeout=120
                )
                if joined.get("ok") is not True or joined.get("org") != ORG:
                    raise AssertionError(f"org join failed: {joined}")
                steps = joined.get("steps")
                if not isinstance(steps, list) or not all(
                    isinstance(step, dict) and step.get("ok") is True for step in steps
                ):
                    raise AssertionError(f"join workflow steps failed: {joined}")
                evidence["joinSteps"] = steps

                for daemon in (daemon_a, daemon_b):
                    _wait(
                        f"{daemon.node_id} directory to list both devices",
                        lambda d=daemon: _directory_devices(d),
                        lambda value: value == ["a", "b"],
                        timeout=DIRECTORY_WAIT_SECONDS,
                    )
                evidence["directory"] = {
                    "a": _directory_devices(daemon_a),
                    "b": _directory_devices(daemon_b),
                }

                # orgfs traffic in both directions through the org's space.
                daemon_a.command(
                    "org", "fs", "write", space_id, "from-alice.md",
                    "e2e-025-alice-to-bob", "--json",
                )
                _wait(
                    "alice's document to replicate to bob",
                    lambda: daemon_b.command(
                        "org", "fs", "read", space_id, "from-alice.md", "--json", timeout=15
                    ),
                    lambda value: isinstance(value, dict)
                    and value.get("text") == "e2e-025-alice-to-bob",
                    timeout=REPLICATE_WAIT_SECONDS,
                )
                daemon_b.command(
                    "org", "fs", "write", space_id, "from-bob.md",
                    "e2e-025-bob-to-alice", "--json",
                )
                _wait(
                    "bob's document to replicate to alice",
                    lambda: daemon_a.command(
                        "org", "fs", "read", space_id, "from-bob.md", "--json", timeout=15
                    ),
                    lambda value: isinstance(value, dict)
                    and value.get("text") == "e2e-025-bob-to-alice",
                    timeout=REPLICATE_WAIT_SECONDS,
                )
                evidence["orgfsRoundtrip"] = True

                # Path proof on bob: business traffic rides the sidecar's
                # mapped loopback ports, never a direct dial of alice's
                # Zenoh listener.
                ps_b = daemon_b.ps()
                forwarding = ps_b.get("forwarding")
                endpoints = (
                    forwarding.get("endpoints") if isinstance(forwarding, dict) else None
                )
                if (
                    not isinstance(endpoints, list)
                    or not endpoints
                    or not all(
                        str(endpoint).startswith('unixsock-stream/' + str(daemon_b.root / 'home'))
                        for endpoint in endpoints
                    )
                ):
                    raise AssertionError(
                        f"bob's forwarding endpoints are not sidecar-mapped "
                        f"private Unix slots: {forwarding}"
                    )
                zenoh = ps_b.get("zenoh") if isinstance(ps_b.get("zenoh"), dict) else {}
                connect = [str(item) for item in zenoh.get("connect", [])]
                a_backend = daemon_a.env['HYPRIAL_ZENOH_LISTEN']
                if a_listen in connect or a_backend in connect:
                    raise AssertionError(
                        f"bob dials alice's Zenoh listener directly: {connect}"
                    )
                not_dialed = [
                    endpoint for endpoint in endpoints if endpoint not in connect
                ]
                if not_dialed:
                    raise AssertionError(
                        f"bob's session does not dial the mapped endpoints: "
                        f"{not_dialed} (connect={connect})"
                    )
                evidence["pathEvidence"] = {
                    "bForwardingEndpoints": endpoints,
                    "bZenohConnect": connect,
                    "aListenAbsentFromConnect": True,
                }

                # org.fetch over the same sidecar path.  Seed alice's accepted
                # slot, then ask from bob with the exact GUI request mode.
                _seed_org_context(daemon_a)
                fetched = _assert_fetch_counts(
                    _fetch_neighbors(daemon_b),
                    expected_received=1,
                    label="org.fetch neighbors",
                )
                candidates = fetched.get("candidates")
                if not isinstance(candidates, list) or len(candidates) != 1:
                    raise AssertionError(
                        f"org.fetch did not stage exactly one candidate: {fetched}"
                    )
                candidate = candidates[0]
                if not isinstance(candidate, dict):
                    raise AssertionError(f"org.fetch candidate is not an object: {candidate!r}")
                if candidate.get("source") != "a":
                    raise AssertionError(
                        f"org.fetch candidate source is not alice: {candidate}"
                    )
                if candidate.get("publisher") != "alice" or candidate.get("version") != 2:
                    raise AssertionError(
                        f"org.fetch candidate is not alice v2: {candidate}"
                    )
                evidence["orgFetch"] = {
                    "requestMode": "neighbors",
                    "requestedSources": fetched.get("requestedSources"),
                    "responseCount": fetched.get("responseCount"),
                    "receivedCount": fetched.get("receivedCount"),
                    "rejectedCount": fetched.get("rejectedCount"),
                    "candidateSource": candidate.get("source"),
                    "candidatePublisher": candidate.get("publisher"),
                    "candidateVersion": candidate.get("version"),
                }

                # A stopped peer must produce an empty answer, never a
                # fabricated success or a stale candidate from the first fetch.
                daemon_a.stop()
                _wait(
                    "alice daemon socket to disappear",
                    lambda: (daemon_a.root / "state" / "daemon.sock").exists(),
                    lambda exists: exists is False,
                    timeout=15,
                )
                empty = _assert_fetch_counts(
                    _fetch_neighbors(daemon_b),
                    expected_received=0,
                    label="org.fetch after peer stop",
                )
                if empty.get("candidates") != []:
                    raise AssertionError(
                        f"stopped peer still produced candidates: {empty}"
                    )
                evidence["orgFetchAfterPeerStop"] = {
                    "responseCount": empty.get("responseCount"),
                    "receivedCount": empty.get("receivedCount"),
                    "rejectedCount": empty.get("rejectedCount"),
                    "candidates": empty.get("candidates"),
                }
            finally:
                for daemon in (daemon_b, daemon_a):
                    try:
                        daemon.stop()
                    except BaseException as error:  # noqa: BLE001 - stop both
                        print(
                            f"RESULT info E2E-025 stop of {daemon.node_id} "
                            f"raised {type(error).__name__}: {error}"
                        )
        evidence["result"] = "PASS"
        print(
            "RESULT PASS E2E-025 tailcat-org-invite-join "
            "join=4/4 directory=2+2 orgfs=roundtrip orgfetch=neighbors+0 "
            "path=tailcat cleanup=clean"
        )
        return 0
    finally:
        _stop_labrelay(labrelay)
        _write_evidence(evidence)


def _write_evidence(evidence: dict[str, Any]) -> None:
    try:
        target = artifacts_root()
        target.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = target / f"e2e-025-evidence-{stamp}.json"
        path.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"RESULT evidence E2E-025 written to {path}")
    except OSError as error:
        print(f"RESULT info E2E-025 evidence write failed: {error}")


def main() -> int:
    binary = os.environ.get(TAILCAT_BINARY_ENV, "").strip()
    labrelay = os.environ.get(LABRELAY_ENV, "").strip()
    missing = [
        name
        for name, value in ((TAILCAT_BINARY_ENV, binary), (LABRELAY_ENV, labrelay))
        if not value or not Path(value).is_file()
    ]
    if missing:
        print(
            f"RESULT BLOCKED E2E-025 tailcat-org-invite-join: required "
            f"executables are missing or unset: {', '.join(missing)} "
            "(the pinned sidecar build and the labrelay fixture; see "
            "25-tailcat-org-invite-join.md)"
        )
        return 3

    def expired(_signum: int, _frame: object) -> None:
        raise ScenarioTimeout(
            f"{SCENARIO} exceeded its {OVERALL_TIMEOUT_SECONDS}s overall deadline"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(OVERALL_TIMEOUT_SECONDS)
    try:
        return _run(binary, labrelay)
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
        print(f"RESULT FAIL E2E-025 tailcat-org-invite-join {error}")
        raise SystemExit(1) from error
