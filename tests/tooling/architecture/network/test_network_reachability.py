"""Negative controls for the conservative IPC-to-network reachability gate."""

from __future__ import annotations

import importlib.util
import json
from collections import defaultdict
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts/ci/check_network_reachability.py"
SPEC = importlib.util.spec_from_file_location("network_reachability", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))


def _fixture(root: Path, *, add_new_path: bool = False) -> None:
    _write(
        root,
        "src/hyprial/daemon/impl/ipc/router.py",
        """
        class _IpcDispatchMixin:
            def handle(self):
                return self._ipc_send()
        """,
    )
    _write(
        root,
        "src/hyprial/daemon/impl/application/send.py",
        """
        class SendMixin:
            def _ipc_send(self):
                return self._deliver()
            def _deliver(self):
                return self._session.put('key', b'value')
        """,
    )
    _write(
        root,
        "src/hyprial/daemon/impl/transport/zenoh/transport.py",
        """
        class ZenohTransport:
            def put(self, key, payload):
                return None
        """,
    )
    if add_new_path:
        _write(
            root,
            "src/hyprial/daemon/impl/application/extra.py",
            """
            class ExtraMixin:
                def _ipc_send(self):
                    return self._session.query('key', b'value')
            """,
        )
        _write(
            root,
            "src/hyprial/daemon/impl/transport/session_actor.py",
            """
            class TransportSessionAuthority:
                def query(self, key, payload):
                    return None
            """,
        )


def test_transitive_ipc_to_transport_path_is_reported(tmp_path: Path) -> None:
    _fixture(tmp_path)
    report = GATE.scan(tmp_path, baseline=frozenset())
    assert report["status"] == "FAIL"
    violation = report["violations"][0]
    assert violation["kind"] == "network-reachable-from-ipc"
    assert violation["root"].endswith("._IpcDispatchMixin.handle")
    assert violation["boundary"].endswith(".ZenohTransport.put")
    assert violation["path"][0] == violation["root"]
    assert violation["path"][-1] == violation["boundary"]


def test_baseline_suppresses_only_the_exact_known_path(tmp_path: Path) -> None:
    _fixture(tmp_path)
    first = GATE.scan(tmp_path, baseline=frozenset())
    known = frozenset({first["violations"][0]["fingerprint"]})
    assert GATE.scan(tmp_path, baseline=known)["status"] == "PASS"

    _fixture(tmp_path, add_new_path=True)
    changed = GATE.scan(tmp_path, baseline=known)
    assert changed["status"] == "FAIL"
    assert any("query" in row["boundary"] for row in changed["violations"])


def test_stale_baseline_entry_fails_until_review_removes_it(tmp_path: Path) -> None:
    _fixture(tmp_path)
    first = GATE.scan(tmp_path, baseline=frozenset())
    known = first["violations"][0]["fingerprint"]

    report = GATE.scan(tmp_path, baseline=frozenset({known, "stale-fingerprint"}))

    assert report["status"] == "FAIL"
    assert report["staleBaseline"] == ["stale-fingerprint"]
    assert {row["kind"] for row in report["violations"]} == {"stale-baseline"}


def test_checked_in_baseline_keeps_repository_gate_green() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert report["status"] == "PASS"
    assert report["knownViolationCount"] > 0


def test_retired_discord_heartbeat_alias_is_not_baselined() -> None:
    document = json.loads((ROOT / "scripts/ci/network-reachability-baseline.json").read_text())
    fingerprint = "ccf5d6a7fa919e13b652ed6b7096cbbc28cb454c65bbe4a5e2194ff3b4e9584c"
    assert fingerprint not in {row["fingerprint"] for row in document["violations"]}
    assert "KP-008" in document["description"]
    assert "HeartbeatSender" in document["description"]
    assert "DiscordTransport.send" in document["description"]
    assert "eaa63db54b374230e4815187a1bccaff24e814452ca502a36a6d1afa4aed7fb1" not in {
        row["fingerprint"] for row in document["violations"]
    }


def test_discord_heartbeat_still_reaches_its_transport() -> None:
    import asyncio
    from hyprial.daemon.impl.adapters.discord.components import HeartbeatSupervisor
    from hyprial.daemon.impl.adapters.discord.provider import DiscordGatewayRuntime

    calls = []
    runtime = DiscordGatewayRuntime.__new__(DiscordGatewayRuntime)
    runtime._last_seen_seq = 17

    class Transport:
        async def send(self, payload):
            calls.append(payload)
            supervisor._generation = 2

    async def sleep(_seconds):
        return None

    async def missed(_generation):
        raise AssertionError("unexpected missed heartbeat")

    runtime._transport = Transport()
    supervisor = HeartbeatSupervisor(send_heartbeat=runtime._send_heartbeat, on_missed_ack=missed,
                                     sleep=sleep, jitter=lambda _low, _high: 0.0)
    supervisor._generation = 1
    asyncio.run(supervisor._run(1, 1.0))
    assert calls == [{"op": 1, "d": 17}]


def test_send_and_lookup_remain_visible_to_network_gate() -> None:
    """Pin both real registry bindings and their scanner-resolved handler edges."""
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
    from hyprial.daemon.impl.request_catalog.daemon.message_lookup import message_send_lookup
    from hyprial.daemon.impl.request_catalog.message.send.specs import message_send

    functions, imports, errors = GATE._load(ROOT)
    assert not errors
    by_short_name: dict[str, set[str]] = defaultdict(set)
    for name in functions:
        by_short_name[name.rsplit(".", 1)[-1]].add(name)
    for method, handler, short_name in (
        ("message.send", message_send, "_ipc_message_send"),
        ("message.send.lookup", message_send_lookup, "_ipc_message_send_lookup"),
    ):
        assert METHOD_REGISTRY.get(method).handler is handler
        name = handler.__module__ + "." + handler.__qualname__
        edges = GATE._resolve_calls(
            functions[name], functions=functions, imports=imports, by_short_name=by_short_name,
        )
        targets = by_short_name[short_name]
        assert targets and targets <= edges, f"registered {method} handler hides {short_name}"


def test_owned_attribute_named_after_its_class_survives_many_same_named_methods(
    tmp_path: Path,
) -> None:
    """More than eight unrelated ``admit`` methods must not erase the real edge."""

    _write(
        tmp_path,
        "src/hyprial/daemon/impl/ipc/router.py",
        """
        class _IpcDispatchMixin:
            def handle(self):
                return self._remote_workflow.admit()
        """,
    )
    _write(
        tmp_path,
        "src/hyprial/daemon/impl/dispatch/remote/__init__.py",
        """
        class RemoteWorkflow:
            def admit(self):
                return self._wire.call()
        """,
    )
    _write(
        tmp_path,
        "src/hyprial/daemon/impl/dispatch/remote/wire.py",
        """
        class RemoteWire:
            def call(self):
                return None
        """,
    )
    for index in range(9):
        _write(
            tmp_path,
            f"src/hyprial/daemon/impl/other/unrelated_{index}.py",
            f"""
            class Unrelated{index}:
                def admit(self):
                    return None
            """,
        )
    report = GATE.scan(tmp_path, baseline=frozenset())
    assert report["status"] == "FAIL"
    assert [row["caller"] for row in report["violations"]] == [
        "hyprial.daemon.impl.dispatch.remote.RemoteWorkflow.admit"
    ]
