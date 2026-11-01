from pathlib import Path
from decimal import Decimal
import pytest

from app.agent.agent import DeliveryAgent
from app.agent.client import MockClient
from app.core.importers import load_batch
from app.core.models import Confirmation
from app.services.task_manager import TaskManager


SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "batch.json"


def test_unreviewed_reference_cannot_close_task_and_file_tampering_reopens(tmp_path):
    manager = TaskManager(tmp_path / "proof.sqlite3", upload_root=tmp_path / "uploads")
    manager.import_batch(load_batch(SAMPLE))
    # ORD-008 has nonempty references in imported JSON, but no trusted review records.
    assert manager.verify(None, "ORD-008").status == "needs_evidence"
    proof = tmp_path / "uploads" / "signed.pdf"
    proof.parent.mkdir()
    proof.write_bytes(b"signed sample")
    confirmation = Confirmation(confirmation_id="CONF-002-NEW", delivery_id="DEL-002",
                                items=[{"sku": "水泥", "quantity": 100}],
                                evidence_ref="uploads/signed.pdf", source="uploads/CONF-002-NEW.json")
    manager.submit_evidence(confirmation=confirmation)
    assert manager.verify(None, "ORD-002").status == "needs_evidence"
    assert manager.review_confirmation(confirmation.source, proof, actor="张审核", page=1,
                                       note="客户签名可辨识").status == "verified"
    review = manager.get_reviews("ORD-002")[0]
    assert review["actor"] == "张审核" and review["page"] == 1 and len(review["file_sha256"]) == 64
    assert review["reviewed_at"]
    proof.write_bytes(b"tampered")
    assert manager.verify(None, "ORD-002").status == "needs_evidence"
    manager.close()


def test_demo_trust_cannot_be_used_for_modified_customer_data(tmp_path):
    manager = TaskManager(tmp_path / "proof.sqlite3")
    altered = load_batch(SAMPLE)
    altered.confirmations[0].evidence_ref = "arbitrary.pdf"
    with pytest.raises(ValueError, match="演示信任"):
        manager.import_batch(altered, trusted_demo=True)
    manager.close()


def test_correction_and_void_keep_old_versions_and_reconcile_tasks(tmp_path):
    manager = TaskManager(tmp_path / "proof.sqlite3")
    manager.import_batch(load_batch(SAMPLE), trusted_demo=True)
    assert {i.code for i in manager.verify(None, "ORD-005").issues} == {"AMOUNT_MISMATCH"}
    delivery = next(d for d in manager.get_batch().deliveries if d.delivery_id == "DEL-005")
    fixed = delivery.model_copy(update={"declared_amount": Decimal("3000")})
    result = manager.correct_document("delivery", delivery.source, fixed,
                                      actor="李审核", reason="原金额录入错误")
    assert result.status == "verified"
    events = manager.get_document_events(delivery.source)
    assert events[0]["old_payload"]["declared_amount"] == "2999"
    assert events[0]["actor"] == "李审核" and events[0]["created_at"]
    assert {i.code for i in manager.verify(None, "ORD-007").issues} == {"DUPLICATE_DELIVERY"}
    result = manager.void_document("delivery", "deliveries/DEL-007-copy.json",
                                   actor="李审核", reason="重复扫描")
    assert result.status == "verified"
    assert manager.get_document_events("deliveries/DEL-007-copy.json")[0]["action"] == "void"
    assert all(t.status == "resolved" for t in manager.get_tasks("ORD-007"))
    manager.close()


def test_agent_failure_is_explicit_and_runs_survive_reopen(tmp_path):
    class BrokenClient:
        def chat(self, messages, tools):
            raise TimeoutError("network timeout")

    database = tmp_path / "proof.sqlite3"
    manager = TaskManager(database)
    manager.import_batch(load_batch(SAMPLE), trusted_demo=True)
    failure = DeliveryAgent(manager, BrokenClient()).run("ORD-001")
    assert failure["stop_reason"] == "model_error:TimeoutError"
    assert "模型调用未完成" in failure["agent_summary"]
    success = DeliveryAgent(manager, MockClient()).run("ORD-001")
    assert success["stop_reason"] == "model_finished" and success["tool_log"]
    manager.close()
    manager = TaskManager(database)
    runs = manager.get_agent_runs("ORD-001")
    assert len(runs) == 2 and runs[0]["stop_reason"] == "model_error:TimeoutError"
    assert runs[1]["tool_log"][0]["tool"] == "query_order"
    assert runs[1]["created_at"] and runs[1]["run_id"]
    assert runs[1]["tool_log"][0]["called_at"]
    manager.close()
