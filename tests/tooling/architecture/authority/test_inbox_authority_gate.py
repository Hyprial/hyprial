from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.gates.inbox_authority_gate import MUTATION_METHODS, scan_bypasses, validate_manifest

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"
MANIFEST = Path(__file__).resolve().parents[4] / 'tests/inbox_authority_exemptions.json'


def test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest() -> None:
    errors = validate_manifest(SRC, MANIFEST)
    assert errors == [], "inbox authority inventory drift:\n" + "\n".join(errors)


@pytest.mark.parametrize("operation", sorted(MUTATION_METHODS))
def test_each_legacy_mutation_bypass_is_detected(
    tmp_path: Path,
    operation: str,
) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class DaemonApplication:\n"
        "    def bypass(self):\n"
        f"        return self._inbox.{operation}()\n",
        encoding="utf-8",
    )
    found = scan_bypasses(tmp_path / "src" / "hyprial")
    assert any(item.operation == operation for item in found)


def test_raw_inbox_service_construction_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text("inbox = InboxService(database, transport)\n", encoding="utf-8")
    found = scan_bypasses(tmp_path / "src" / "hyprial")
    assert [item.operation for item in found] == ["construct"]


def test_raw_alarm_emitter_access_is_detected(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def quarantine(self):\n    return self._inbox.alarm_emitter\n",
        encoding="utf-8",
    )
    found = scan_bypasses(tmp_path / "src" / "hyprial")
    assert [item.operation for item in found] == ["alarm_emitter"]


def test_import_alias_construction_and_mutation_are_detected(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from hyprial.inbox import InboxService as Store\n"
        "def bypass(database, transport):\n"
        "    delivery = Store(database, transport)\n"
        "    return delivery.ack('agent:a', 'message-1')\n",
        encoding="utf-8",
    )
    found = scan_bypasses(tmp_path / "src" / "hyprial")
    assert {(item.operation, item.owner) for item in found} == {
        ("construct", "bypass"),
        ("ack", "bypass"),
    }


def test_provenance_does_not_leak_across_function_bindings(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from hyprial.inbox import InboxService as Store\n"
        "def legacy(database, transport):\n"
        "    delivery = Store(database, transport)\n"
        "    return delivery\n"
        "def live(delivery):\n"
        "    return delivery.ack('agent:a', 'message-1')\n",
        encoding="utf-8",
    )
    found = scan_bypasses(tmp_path / "src" / "hyprial")
    assert [(item.owner, item.operation) for item in found] == [
        ("legacy", "construct")
    ]


def test_final_cutover_shape_has_no_bypass(tmp_path: Path) -> None:
    source = tmp_path / "src" / "hyprial" / "daemon" / "application.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from hyprial.inbox import DeliveryCustodyCoordinator, DeliveryCustodyFacade\n"
        "def compose(database, transport, events):\n"
        "    coordinator = DeliveryCustodyCoordinator(database, transport, events)\n"
        "    return DeliveryCustodyFacade(coordinator, database)\n",
        encoding="utf-8",
    )
    assert scan_bypasses(tmp_path / "src" / "hyprial") == ()
