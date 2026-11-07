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


def test_send_and_lookup_remain_visible_to_network_gate() -> None:
    functions, imports, errors = GATE._load(ROOT)
    assert not errors
    by_short_name: dict[str, set[str]] = defaultdict(set)
    for name in functions:
        by_short_name[name.rsplit(".", 1)[-1]].add(name)
    root = "hyprial.daemon.impl.ipc.router._IpcDispatchMixin.handle"
    edges = GATE._resolve_calls(
        functions[root], functions=functions, imports=imports,
        by_short_name=by_short_name,
    )
    for method in ("_ipc_message_send",):
        targets = by_short_name[method]
        assert targets and targets <= edges, f"IPC route hides {method} from the network gate"
    # message.send.lookup is dispatched by the registry, outside the router
    # root's resolvable body; KP-008 (docs/reference/known-problems.md) owns
    # restoring root-level gate visibility. Pin the declared dispatch chain:
    handler = functions[
        "hyprial.daemon.impl.request_catalog.daemon.message_lookup.message_send_lookup"
    ]
    handler_edges = GATE._resolve_calls(
        handler, functions=functions, imports=imports, by_short_name=by_short_name,
    )
    assert by_short_name["_ipc_message_send_lookup"] <= handler_edges
