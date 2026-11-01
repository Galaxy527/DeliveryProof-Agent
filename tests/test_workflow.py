import json
from pathlib import Path

from app.core.models import Batch, Confirmation
from app.core.importers import load_batch
from app.core.validator import validate_order
from app.services.task_manager import TaskManager
from app.services.workflow import submit_confirmation


SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "batch.json"


def sample() -> Batch:
    return Batch.model_validate_json(SAMPLE.read_text(encoding="utf-8"))


def test_ten_cases_and_rule_results():
    batch = sample()
    assert len(batch.orders) == 10
    expected = {
        "ORD-001": {"MISSING_SIGNATURE", "ORDER_QTY_MISMATCH:水泥"},
        "ORD-002": {"MISSING_SIGNATURE"},
        "ORD-003": {"ORDER_QTY_MISMATCH:水泥"},
        "ORD-004": {"PRICE_MISMATCH:水泥"},
        "ORD-005": {"AMOUNT_MISMATCH"},
        "ORD-006": {"ACCEPTED_QTY_MISMATCH:水泥"},
        "ORD-007": {"DUPLICATE_DELIVERY"},
        "ORD-008": set(),
        "ORD-009": set(),
        "ORD-010": {"ORDER_QTY_MISMATCH:水泥"},
    }
    for order_id, codes in expected.items():
        issues, log = validate_order(batch, order_id)
        assert {issue.code for issue in issues} == codes
        assert all(issue.sources and issue.action for issue in issues)
        assert log[0].step == "match_documents"


def test_tasks_are_idempotent_and_reverification_resolves(tmp_path):
    batch = sample()
    manager = TaskManager(tmp_path / "tasks.sqlite3", upload_root=tmp_path / "uploads")
    first = manager.verify(batch, "ORD-002")
    repeated = manager.verify(batch, "ORD-002")
    assert len(first.tasks) == len(repeated.tasks) == 1
    assert repeated.tasks[0].status == "open"
    proof = tmp_path / "uploads" / "proof.pdf"
    proof.parent.mkdir(exist_ok=True)
    proof.write_bytes(b"sample signed proof")
    result = submit_confirmation(batch, Confirmation(
        confirmation_id="CONF-002-B", delivery_id="DEL-002",
        items=[{"sku": "水泥", "quantity": "100"}],
        evidence_ref="uploads/proof.pdf", source="confirmations/CONF-002-B.json"
    ), manager)
    assert result.status == "needs_evidence"
    result = manager.review_confirmation("confirmations/CONF-002-B.json", proof,
                                         actor="验收员", page=1, note="核对签收")
    assert result.status == "verified"
    assert result.tasks[0].status == "resolved"
    assert any(event.step == "resolve_evidence_task" for event in result.log)
    manager.close()


def test_duplicate_document_does_not_inflate_quantity():
    issues, _ = validate_order(sample(), "ORD-007")
    assert {issue.code for issue in issues} == {"DUPLICATE_DELIVERY"}


def test_order_without_delivery_waits_for_source_document():
    batch = sample()
    batch.deliveries = [delivery for delivery in batch.deliveries if delivery.order_id != "ORD-008"]
    issues, _ = validate_order(batch, "ORD-008")
    assert [issue.code for issue in issues] == ["MISSING_DELIVERY"]
    assert "仓库" in issues[0].action


def test_csv_matches_json_sample():
    json_batch = load_batch(SAMPLE)
    csv_batch = load_batch(SAMPLE.parent / "csv")
    assert csv_batch.model_dump(mode="json") == json_batch.model_dump(mode="json")


def test_ord001_persistent_evidence_and_history(tmp_path):
    db = tmp_path / "proof.sqlite3"
    manager = TaskManager(db, upload_root=tmp_path / "uploads")
    first = manager.verify(sample(), "ORD-001")
    assert len(first.issues) == 2
    manager.close()

    manager = TaskManager(db, upload_root=tmp_path / "uploads")
    confirmation = Confirmation.model_validate_json(
        (SAMPLE.parent / "ord001_confirmation.json").read_text(encoding="utf-8"))
    proof_a = tmp_path / "uploads" / "proof-a.pdf"
    proof_a.parent.mkdir(exist_ok=True)
    proof_a.write_bytes(b"confirmation a")
    confirmation.evidence_ref = "uploads/proof-a.pdf"
    manager.submit_evidence(confirmation=confirmation)
    second = manager.review_confirmation(confirmation.source, proof_a,
                                         actor="验收员", page=1, note="核对签收")
    assert {i.code for i in second.issues} == {"ORDER_QTY_MISMATCH:水泥"}
    manager.close()

    from app.core.models import Delivery
    manager = TaskManager(db, upload_root=tmp_path / "uploads")
    delivery = Delivery.model_validate_json(
        (SAMPLE.parent / "ord001_supplement_delivery.json").read_text(encoding="utf-8"))
    confirmation = Confirmation.model_validate_json(
        (SAMPLE.parent / "ord001_supplement_confirmation.json").read_text(encoding="utf-8"))
    proof_b = tmp_path / "uploads" / "proof-b.pdf"
    proof_b.write_bytes(b"confirmation b")
    confirmation.evidence_ref = "uploads/proof-b.pdf"
    manager.submit_evidence(delivery=delivery, confirmation=confirmation)
    final = manager.review_confirmation(confirmation.source, proof_b,
                                        actor="验收员", page=1, note="核对签收")
    assert final.status == "verified"
    assert len(final.tasks) == 3 and all(t.status == "resolved" for t in final.tasks)
    assert len(manager.get_history("ORD-001")) >= 3
    manager.close()
