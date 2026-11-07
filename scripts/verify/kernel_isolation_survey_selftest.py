#!/usr/bin/env python3
"""Self-test the kernel-isolation survey scanner against synthetic shapes."""

from __future__ import annotations

import ast
import collections
import tempfile
import textwrap
from pathlib import Path

import kernel_isolation_survey as survey


FIXTURE = textwrap.dedent(
    """
    import socketserver
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
    from http.server import ThreadingHTTPServer
    import threading
    import time
    from typing import Callable, Protocol, TypeVar

    T = TypeVar("T")
    class ThreadedBase(socketserver.ThreadingMixIn, socketserver.TCPServer):
        pass
    class ThreadedChild(ThreadedBase):
        pass
    DynamicServer = type(
        "DynamicServer",
        (socketserver.ThreadingMixIn, socketserver.TCPServer),
        {},
    )

    def execution_shapes(address, handler):
        threading.Thread(target=lambda: None)
        threading.Timer(1.0, lambda: None)
        ThreadPoolExecutor()
        ProcessPoolExecutor()
        ThreadingHTTPServer(address, handler)

    def retry_submit(port):
        while True:
            result = port.submit("x")
            if result == "accepted": return
            time.sleep(0.01)
    def retry_tell(port):
        while True:
            result = port.tell("x")
            if result == "accepted": return
            time.sleep(0.01)
    def retry_admit(port):
        while True:
            result = port.admit("x")
            if result == "accepted": return
            time.sleep(0.01)
    def retry_connect(port):
        while True:
            port.connect()
            time.sleep(0.01)
    def retry_open(port):
        while True:
            port.open()
            time.sleep(0.01)
    def retry_begin(port):
        while True:
            port.begin()
            time.sleep(0.01)
    def retry_request(port):
        for attempt in range(2):
            try:
                return port.request()
            except ConnectionError:
                if attempt:
                    raise
    def failover_holders(mesh, holders):
        index = 0
        while index < len(holders):
            holder = holders[index]
            try:
                return mesh.sync_from(holder)
            except ConnectionError:
                index += 1
                continue
        return None
    def failover_holders_with_deadline(mesh, holders, deadline):
        refreshed = True
        for holder in holders:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                mesh.sync_from(holder, timeout=remaining)
            except ConnectionError:
                refreshed = False
        return refreshed
    def periodic_reentry(service, stop):
        while not stop.wait(1.0):
            service.tick()
    def observe_only(reader):
        while True:
            state = reader.read()
            if state == "done": return state
            time.sleep(0.01)

    class CallableProtocol(Protocol):
        def __call__(self, value: int) -> None: ...
    class GenericPort(Protocol[T]):
        def submit(self, value: T) -> bool: ...
    class Service(Protocol):
        def status(self) -> str: ...
    CallbackAlias = Callable[[int], None]
    def carries_callback(
        port: GenericPort[int], callback: CallbackAlias
    ) -> None: ...
    """
)


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="kernel-survey-selftest-", dir=survey.ROOT
    ) as temp:
        path = Path(temp) / "fixture.py"
        path.write_text(FIXTURE, encoding="utf-8")
        execution, _sync = survey.execution_sites([path])
        kinds = collections.Counter(row[2] for row in execution)
        required_kinds = {
            "thread",
            "timer-thread",
            "thread-pool",
            "process-pool",
            "per-request-thread-server",
            "inherited-thread-spawner",
        }
        missing_kinds = required_kinds - set(kinds)
        if missing_kinds or kinds["inherited-thread-spawner"] < 3:
            raise SystemExit(
                f"execution self-test missed {sorted(missing_kinds)}; "
                f"inheritance={kinds['inherited-thread-spawner']}"
            )

        retry_rows = {
            row[3]: row[5] for row in survey.retry_candidates([path])
        }
        failover_names = {
            "failover_holders",
            "failover_holders_with_deadline",
        }
        retry_names = {
            "retry_submit",
            "retry_tell",
            "retry_admit",
            "retry_connect",
            "retry_open",
            "retry_begin",
            "retry_request",
            "periodic_reentry",
            *failover_names,
        }
        missed_retries = {
            name
            for name in retry_names
            if "shape=retry" not in retry_rows.get(name, "")
        }
        if missed_retries:
            raise SystemExit(f"retry self-test missed {sorted(missed_retries)}")
        missed_failovers = {
            name
            for name in failover_names
            if "shape=retry-failover" not in retry_rows.get(name, "")
        }
        if missed_failovers:
            raise SystemExit(
                "failover self-test missed simple/deadline fixtures: "
                f"{sorted(missed_failovers)}"
            )
        if "shape=observation" not in retry_rows.get("observe_only", ""):
            raise SystemExit("retry self-test confused observation with replay")

        baseline_candidates = survey.retry_candidates()
        baseline_dispositions = survey.retry_dispositions(baseline_candidates)
        link_loop = next(
            (
                row
                for row in baseline_dispositions
                if row[0].endswith("links/service/core.py")
                and row[2].endswith("LinkService._loop")
            ),
            None,
        )
        if link_loop is None or link_loop[4] != "limit-class:periodic-reentry":
            raise SystemExit("limit-class self-test missed LinkService periodic re-entry")
        service_reconcile = next(
            (
                row
                for row in baseline_dispositions
                if row[0].endswith("service_connect/manager.py")
                and row[2].endswith("ServiceManager._scheduled_reconcile")
            ),
            None,
        )
        if (
            service_reconcile is None
            or service_reconcile[4] != "limit-class:periodic-reentry"
        ):
            raise SystemExit("limit-class self-test missed ServiceManager periodic re-entry")
        details = survey.limit_class_details()
        service_detail = next(
            (row for row in details if row[2] == "_scheduled_reconcile"), None
        )
        if service_detail is None or not all(service_detail[3:]):
            raise SystemExit("limit-class self-test missed ServiceManager detail fields")
        if service_detail[1] != (
            "src/hyprial/daemon/impl/service_connect/manager.py:494"
        ):
            raise SystemExit("limit-class self-test lost ServiceManager file:line")
        service_detail_text = " ".join(service_detail[3:])
        required_service_evidence = (
            "5.0s",
            "3.0s",
            "capacity 16",
            "one worker",
            "manager RLock",
            "no attempt or aggregate pass deadline",
            "fingerprint-deduplicated",
            "service.list/connect/disconnect",
        )
        if not all(
            fragment in service_detail_text for fragment in required_service_evidence
        ):
            raise SystemExit("limit-class self-test lost ServiceManager semantics")
        limit_lines = {row[1] for row in survey.limit_class_instances()}
        required_limit_lines = {
            "src/hyprial/daemon/impl/links/service/core.py:500",
            "src/hyprial/daemon/impl/links/service/core.py:189",
            "src/hyprial/daemon/impl/links/store.py:625",
            "src/hyprial/daemon/impl/links/store.py:666",
        }
        if not required_limit_lines <= limit_lines:
            raise SystemExit(
                "limit-class self-test missed baseline instances: "
                f"{sorted(required_limit_lines - limit_lines)}"
            )

        tree = survey.parse(path)
        definitions = {
            node.name: node
            for node in tree.body
            if isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
        }
        for name in ("CallableProtocol", "GenericPort", "carries_callback"):
            if not survey._callback_bearing(path, definitions[name]):
                raise SystemExit(f"callback self-test missed {name}")
        if survey._callback_bearing(path, definitions["Service"]):
            raise SystemExit(
                "callback self-test mislabeled a non-callback Protocol"
            )

    print(
        "self-test: PASS; execution primitives+inheritance, retry/failure/"
        "failover/periodic-reentry shapes, limit classes, observation wait, "
        "and callback types detected"
    )


if __name__ == "__main__":
    main()
