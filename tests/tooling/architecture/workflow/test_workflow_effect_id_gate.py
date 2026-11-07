from __future__ import annotations

from pathlib import Path
import inspect
from hyprial.daemon.impl.inbox.actor.custody import DeliveryCustody

from tests.support.gates.workflows.workflow_effect_id_gate import scan_delivery_receipt_gate, scan_source, scan_workflow_inbox_adapter


ROOT = Path(__file__).resolve().parents[4]
ADAPTER = ROOT / "src/hyprial/daemon/impl/inbox/links/io.py"


class _NativeCustodySource:
    """Read actual resolved custody methods for the original class-shaped gate."""
    def __str__(self):
        return "DeliveryCustody actual native MRO"

    def read_text(self, encoding="utf-8"):
        methods = {}
        for owner in DeliveryCustody.__mro__:
            for name, function in vars(owner).items():
                if inspect.isfunction(function) and name not in methods:
                    methods[name] = inspect.getsource(function)
        if not methods:
            raise RuntimeError("No native custody methods")
        return "class DeliveryCustody:\n" + "\n".join(methods.values())


DELIVERY = _NativeCustodySource()


def test_workflow_inbox_adapter_preserves_effect_id_at_every_inbox_seam() -> None:
    assert scan_workflow_inbox_adapter(ADAPTER) == []


def test_uuid4_reverse_mutation_is_rejected() -> None:
    source = ADAPTER.read_text(encoding="utf-8").replace(
        "self.message_id(effect_id)",
        "uuid4()",
        1,
    )
    errors = scan_source(source)
    assert "delivery message id must not use uuid4" in errors
    assert "delivery message id must derive from effect_id" in errors


def test_deleted_effect_id_reverse_mutation_is_rejected() -> None:
    source = ADAPTER.read_text(encoding="utf-8").replace(
        "message_id = self.message_id(effect_id)",
        "del effect_id\n        message_id = self.message_id(effect_id)",
        1,
    )
    assert "delivery adapter must not delete effect_id" in scan_source(source)


def test_delivery_authority_reads_and_persists_d22_receipt() -> None:
    assert scan_delivery_receipt_gate(DELIVERY) == []


def test_d22_receipt_reverse_mutation_is_rejected(tmp_path: Path) -> None:
    source = DELIVERY.read_text(encoding="utf-8").replace(
        "self._persist_submission_receipt_locked(",
        "self._drop_submission_receipt(",
    )
    mutated = tmp_path / "actor.py"
    mutated.write_text(source, encoding="utf-8")
    assert (
        "delivery outcome must persist its durable submission receipt"
        in scan_delivery_receipt_gate(mutated)
    )
