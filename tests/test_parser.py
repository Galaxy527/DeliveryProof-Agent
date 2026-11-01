import json

import pymupdf as fitz

from app.services.document_parser import extract_document, finalize_draft


class CaptureClient:
    def __init__(self, response):
        self.response = response
        self.messages = None

    def chat(self, messages, tools=None):
        self.messages = messages
        return {"content": json.dumps(self.response)}


def test_scanned_pdf_sends_every_page_and_requires_review():
    pdf = fitz.open()
    for _ in range(2):
        page = pdf.new_page()
        page.draw_rect(fitz.Rect(10, 10, 100, 100), color=(0, 0, 0))
    client = CaptureClient({"confirmation_id": "C-1", "delivery_id": "D-1",
                            "items": [{"sku": "水泥", "quantity": "5"}],
                            "evidence_ref": "invented-signature.pdf",
                            "review_notes": ["signature uncertain"],
                            "field_confidence": {"delivery_id": 0.5},
                            "source_pages": {"delivery_id": 2}})
    result = extract_document(pdf.tobytes(), "scan.pdf", "confirmation", client)
    parts = client.messages[0]["content"]
    assert len([p for p in parts if p["type"] == "image_url"]) == 2
    assert result["draft"]["evidence_ref"] is None
    assert result["requires_review"]
    assert result["source_pages"]["delivery_id"] == 2


def test_text_pdf_includes_page_markers():
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Order ORD-123 customer Zhang cement quantity 10 unit price 30")
    client = CaptureClient({"order_id": "ORD-123", "customer": "Zhang",
                            "items": [{"sku": "水泥", "quantity": "10", "unit_price": "30"}]})
    result = extract_document(pdf.tobytes(), "order.pdf", "order", client)
    assert "[PAGE 1]" in client.messages[0]["content"][0]["text"]
    assert len([part for part in client.messages[0]["content"] if part["type"] == "image_url"]) == 1
    assert result["draft"]["source"].startswith("uploads/")


def test_uploaded_confirmation_closes_task_only_after_review(tmp_path):
    from pathlib import Path
    from app.core.importers import load_batch
    from app.services.task_manager import TaskManager

    root = Path(__file__).resolve().parents[1]
    manager = TaskManager(tmp_path / "proof.sqlite3", upload_root=tmp_path / "uploads")
    manager.import_batch(load_batch(root / "data" / "samples" / "batch.json"), trusted_demo=True)
    assert manager.verify(None, "ORD-002").status == "needs_evidence"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Customer confirmation DEL-002 cement 100 bags signed")
    client = CaptureClient({"confirmation_id": "CONF-002-NEW", "delivery_id": "DEL-002",
                            "items": [{"sku": "水泥", "quantity": "100"}],
                            "evidence_ref": "fake-file.pdf"})
    extracted = extract_document(pdf.tobytes(), "proof.pdf", "confirmation", client)
    reviewed = finalize_draft(extracted, extracted["draft"], signed_confirmed=True)
    assert reviewed.evidence_ref == extracted["source"]
    proof_path = tmp_path / reviewed.evidence_ref
    proof_path.parent.mkdir(exist_ok=True)
    proof_path.write_bytes(pdf.tobytes())
    assert manager.submit_evidence(confirmation=reviewed).status == "needs_evidence"
    assert manager.review_confirmation(reviewed.source, proof_path, actor="验收员", page=1,
                                       note="对照原件确认客户签收").status == "verified"
    manager.close()
