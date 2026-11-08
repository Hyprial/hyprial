"""Executable contract for the unbounded blocking-call CI gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[4] / "scripts/ci/check_unbounded_waits.py"
SPEC = importlib.util.spec_from_file_location("unbounded_waits", SCRIPT)
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class UnboundedWaitGateTests(unittest.TestCase):
    def tree(self) -> Path:
        directory = tempfile.TemporaryDirectory(prefix="hyprial-unbounded-waits-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for relative in GATE.SCAN_ROOTS:
            (root / relative).mkdir(parents=True)
        return root

    def source(self, root: Path, body: str, name: str = "probe.py") -> Path:
        path = root / GATE.SCAN_ROOTS[0] / name
        path.write_text(body, encoding="utf-8")
        return path

    def baseline(self, root: Path, entries: list[dict[str, object]]) -> Path:
        path = root / GATE.BASELINE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": GATE.BASELINE_VERSION, "entries": entries}, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @staticmethod
    def entry(row: dict[str, object]) -> dict[str, object]:
        return {
            "path": row["path"],
            "line": row["line"],
            "method": row["method"],
            "call": row["call"],
            "reason": "existing debt recorded when the gate was introduced",
        }

    def test_flags_unbounded_blocking_calls(self) -> None:
        root = self.tree()
        self.source(
            root,
            """\
def blocked(event, queue, future, thread, lock, condition):
    event.wait()
    queue.get()
    future.result()
    thread.join()
    lock.acquire()
    condition.wait()
    queue.get(block=True)
    queue.get(True)
""",
        )
        report = GATE.scan(root)
        self.assertEqual(
            [row["method"] for row in report["violations"]],
            ["wait", "get", "result", "join", "acquire", "wait", "get", "get"],
            report,
        )
        self.assertEqual(report["status"], "FAIL")

    def test_bounded_or_nonblocking_calls_pass(self) -> None:
        root = self.tree()
        self.source(
            root,
            """\
def bounded(event, queue, future, thread, lock, condition, timeout):
    event.wait(timeout)
    queue.get(timeout=timeout)
    queue.get(block=False)
    queue.get(False)
    queue.get(True, timeout)
    future.result(timeout=timeout)
    thread.join(timeout=timeout)
    lock.acquire(timeout=timeout)
    lock.acquire(blocking=False)
    condition.wait(timeout=timeout)
""",
        )
        report = GATE.scan(root)
        self.assertEqual(report["status"], "PASS", report)

    def test_none_and_block_true_are_still_unbounded(self) -> None:
        root = self.tree()
        self.source(
            root,
            """\
def blocked(queue, future, thread, lock):
    queue.get(timeout=None)
    queue.get(block=True, timeout=None)
    future.result(None)
    thread.join(None)
    lock.acquire(True, -1)
""",
        )
        report = GATE.scan(root)
        self.assertEqual(len(report["violations"]), 5, report)

    def test_mapping_get_with_a_key_is_not_a_queue_wait(self) -> None:
        root = self.tree()
        self.source(
            root,
            """\
def lookup(mapping, key):
    return mapping.get(key), mapping.get(key, -1)
""",
        )
        self.assertEqual(GATE.scan(root)["status"], "PASS")

    def test_header_allowlist_requires_a_reason_and_lives_at_file_top(self) -> None:
        root = self.tree()
        self.source(
            root,
            """\
# hyprial-unbounded-wait-allow: join - interpreter shutdown happens after every worker received stop
import threading

def shutdown(thread):
    thread.join()
""",
        )
        report = GATE.scan(root)
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(report["headerAllowed"][0]["method"], "join")

    def test_inline_or_reasonless_allowlist_cannot_hide_a_wait(self) -> None:
        for body in (
            "def blocked(event):\n    event.wait()  # hyprial-unbounded-wait-allow: wait - late\n",
            "# hyprial-unbounded-wait-allow: wait\ndef blocked(event):\n    event.wait()\n",
        ):
            with self.subTest(body=body):
                root = self.tree()
                self.source(root, body)
                report = GATE.scan(root)
                self.assertEqual(report["status"], "FAIL", report)
                self.assertTrue(report["violations"] or report["headerErrors"], report)

    def test_baseline_ignores_line_shifts_for_the_same_call(self) -> None:
        root = self.tree()
        self.source(root, "def blocked(event):\n    event.wait()\n")
        raw = GATE.scan(root, baseline=False)
        self.baseline(root, [self.entry(raw["violations"][0])])

        self.source(root, "\ndef blocked(event):\n    event.wait()\n")
        moved = GATE.scan(root)

        self.assertEqual(moved["status"], "PASS", moved)
        self.assertEqual(moved["violations"], [])
        self.assertEqual(len(moved["grandfathered"]), 1)
        self.assertEqual(moved["grandfathered"][0]["line"], 3)

    def test_new_matching_call_beyond_baseline_count_is_red(self) -> None:
        root = self.tree()
        self.source(root, "def blocked(event):\n    event.wait()\n")
        raw = GATE.scan(root, baseline=False)
        self.baseline(root, [self.entry(raw["violations"][0])])

        self.source(
            root,
            "def blocked(event):\n    event.wait()\n    event.wait()\n",
        )
        report = GATE.scan(root)

        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(len(report["grandfathered"]), 1)
        self.assertEqual(len(report["violations"]), 1)
        self.assertEqual(report["violations"][0]["line"], 3)
        self.assertEqual(report["staleBaseline"], [])

    def test_repaired_baseline_row_is_red_until_removed(self) -> None:
        root = self.tree()
        self.source(root, "def blocked(event):\n    event.wait()\n")
        raw = GATE.scan(root, baseline=False)
        self.baseline(root, [self.entry(raw["violations"][0])])
        self.source(root, "def bounded(event):\n    event.wait(1)\n")
        report = GATE.scan(root)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(len(report["staleBaseline"]), 1)

    def test_unused_header_allowlist_is_red(self) -> None:
        root = self.tree()
        self.source(
            root,
            "# hyprial-unbounded-wait-allow: wait - legacy ownership protocol\n"
            "def bounded(event):\n    event.wait(1)\n",
        )
        report = GATE.scan(root)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertEqual(len(report["staleHeaderAllowlist"]), 1)

    def test_syntax_errors_fail_closed(self) -> None:
        root = self.tree()
        self.source(root, "def broken(:\n")
        report = GATE.scan(root)
        self.assertEqual(report["status"], "FAIL", report)
        self.assertTrue(report["parseErrors"])

    def test_ci_runs_the_gate_before_dependency_install(self) -> None:
        workflow = (SCRIPT.parents[2] / ".forgejo/workflows/tests.yml").read_text(
            encoding="utf-8"
        )
        command = (
            "uv run --no-project --python 3.12 python "
            "scripts/ci/check_unbounded_waits.py"
        )
        self.assertIn(command, workflow)
        self.assertLess(workflow.index(command), workflow.index("Install Python and locked dependencies"))


if __name__ == "__main__":
    unittest.main()
