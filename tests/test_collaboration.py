"""A multi-party case must enforce scope and close only the issues evidenced."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.models import Order
from app.services.collaboration import CollaborationService


def case(tmp_path, *, due_offset=2):
    service = CollaborationService(tmp_path / "case.sqlite3", root=tmp_path)
    order = Order(order_id="SO-001", customer="东区工地", items=[
        {"sku": "水泥", "quantity": "100", "unit_price": "30"}],
        source="manual/orders/SO-001.json",
        delivery_due_date=date.today() + timedelta(days=due_offset),
        allow_split_delivery=True)
    contacts = {
        "supplier": {"name": "赵销售", "email": "supplier@example.test", "phone": "+8613800000000",
                     "contact_allowed": True},
        "driver": {"name": "王配送", "email": "driver@example.test", "phone": "+8613800000001",
                   "contact_allowed": True},
        "receiver": {"name": "李收货", "email": "receiver@example.test", "phone": "+8613800000002",
                     "contact_allowed": True},
    }
    tokens = service.create_order(order, contacts)
    return service, tokens


def test_role_scope_split_delivery_and_receipt_close_independently(tmp_path):
    service, tokens = case(tmp_path)
    driver, receiver = tokens["driver"], tokens["receiver"]
    assert service.participant(driver)["role"] == "driver"
    with pytest.raises(PermissionError):
        service.record_delivery(receiver, "DN-001", Decimal("95"))
    with pytest.raises(PermissionError):
        service.record_receipt(driver, "DN-001", Decimal("95"), condition="accepted")
    service.record_delivery(driver, "DN-001", Decimal("95"))
    first = service.run_agent("SO-001")
    assert {i["code"] for i in first["issues"]} == {"MISSING_SIGNATURE"}
    service.record_receipt(receiver, "DN-001", Decimal("95"), condition="accepted")
    assert {i["code"] for i in service.run_agent("SO-001")["issues"]} == {"MISSING_SIGNATURE"}
    service.review_receipt("SO-001", "DN-001", "signed.pdf", b"sample signed document",
                           note="已对照原始签收单与收货数量")
    assert service.run_agent("SO-001")["status"] == "verified"
    service.record_delivery(driver, "DN-002", Decimal("5"))
    assert {i["code"] for i in service.run_agent("SO-001")["issues"]} == {"MISSING_SIGNATURE"}
    service.record_receipt(receiver, "DN-002", Decimal("5"), condition="accepted")
    service.review_receipt("SO-001", "DN-002", "signed-2.pdf", b"second signed document",
                           note="已对照第二张原始签收单")
    assert service.run_agent("SO-001")["status"] == "verified"
    assert all(task.status == "resolved" for task in service.manager.get_tasks("SO-001"))
    service.close()


def test_due_reminders_are_idempotent_and_invites_rotate(tmp_path):
    service, tokens = case(tmp_path, due_offset=1)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("95"))
    arrived = datetime.now(timezone.utc)
    after_day = arrived + timedelta(hours=25)
    service.run_agent("SO-001", now=after_day)
    service.run_agent("SO-001", now=after_day)
    notes = [n for n in service.notifications("SO-001") if "待处理" in n["subject"]]
    assert len(notes) == 1
    assert notes[0]["role"] == "receiver"
    assert notes[0]["status"] == "queued"
    assert service.dispatch_queued() == 0
    service.run_agent("SO-001", now=arrived + timedelta(days=5))
    notes = [n for n in service.notifications("SO-001") if "邀请" not in n["subject"]]
    assert {note["role"] for note in notes} == {"driver", "receiver"}
    new_token = service.invite("SO-001", "receiver", "李收货", "receiver@example.test",
                               contact_allowed=True)
    with pytest.raises(PermissionError):
        service.participant(tokens["receiver"])
    assert service.participant(new_token)["order_id"] == "SO-001"
    service.close()


def test_receiver_cannot_confirm_other_order_or_overstate_quantity(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("95"))
    with pytest.raises(ValueError):
        service.record_receipt(tokens["receiver"], "DN-001", Decimal("100"), condition="accepted")
    with pytest.raises(PermissionError):
        service.record_receipt(tokens["receiver"], "DN-OTHER", Decimal("1"), condition="accepted")
    service.close()


def test_refusal_remains_open_until_proof_review(tmp_path):
    service, tokens = case(tmp_path)
    arrived = datetime.now(timezone.utc)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("100"), now=arrived)
    service.run_agent("SO-001", now=arrived + timedelta(hours=25))
    pending = [n for n in service.notifications("SO-001") if "待处理" in n["subject"]]
    assert len(pending) == 1 and pending[0]["status"] == "queued"
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("0"),
                           condition="refused", note="包装严重破损", now=arrived + timedelta(hours=26))
    issues = {i["code"] for i in service.run_agent("SO-001")["issues"]}
    assert "MISSING_SIGNATURE" in issues
    assert any(code.startswith("RECEIPT_EXCEPTION") for code in issues)
    assert any(code.startswith("ACCEPTED_QTY_MISMATCH") for code in issues)
    after = service.notifications("SO-001")
    assert any(n["status"] == "queued" for n in after)
    service.close()


def test_business_resolution_keeps_original_rule_finding_and_cancels_reminder(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("90"))
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("85"),
                           condition="partial", note="现场只收到 85 袋")
    result = service.run_agent("SO-001")
    mismatch = next(i for i in result["issues"] if i["code"].startswith("ACCEPTED_QTY_MISMATCH"))
    service.resolve_issue("SO-001", mismatch["issue_id"],
                          "双方核对后确认五袋退回仓库，附现场退货记录", "return.pdf", b"return proof")
    assert any(r["issue_id"] == mismatch["issue_id"] for r in service.resolutions("SO-001"))
    assert mismatch["issue_id"] in {i["issue_id"] for i in service.run_agent("SO-001")["issues"]}
    assert any(n["status"] == "canceled" for n in service.notifications("SO-001")
               if n["role"] == "driver" and "待处理" in n["subject"])
    evidence = service.resolutions("SO-001")[0]["evidence_path"]
    (tmp_path / evidence).write_bytes(b"tampered")
    assert service.resolutions("SO-001") == []
    service.run_agent("SO-001")
    assert any(n["status"] == "queued" for n in service.notifications("SO-001")
               if n["role"] == "driver" and "待处理" in n["subject"])
    service.close()


def test_configured_email_uses_scoped_link_without_network_in_test(tmp_path, monkeypatch):
    monkeypatch.setenv("DELIVERYPROOF_PUBLIC_URL", "https://delivery.example.test")
    monkeypatch.setenv("DELIVERYPROOF_AUTO_EMAIL", "1")
    sent = []
    monkeypatch.setattr(CollaborationService, "_send_email",
                        staticmethod(lambda to, subject, body: sent.append((to, subject, body))))
    service, tokens = case(tmp_path)
    assert service.dispatch_queued() == 2
    assert {recipient for recipient, _, _ in sent} == {
        "driver@example.test", "receiver@example.test"}
    assert tokens["driver"] in next(body for to, _, body in sent if to == "driver@example.test")
    assert tokens["receiver"] not in next(body for to, _, body in sent if to == "driver@example.test")
    assert all("https://delivery.example.test/?token=" in body for _, _, body in sent)
    service.close()


def test_partial_receipt_keeps_both_original_accounts_and_manual_notice(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("90"))
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("85"),
                           condition="partial", note="现场只收到 85 袋")
    result = service.run_agent("SO-001")
    assert any("部分接收" in issue["detail"] for issue in result["issues"])
    mismatch = next(issue for issue in result["issues"]
                    if issue["code"].startswith("ACCEPTED_QTY_MISMATCH"))
    assert "保留双方原始记录" in mismatch["action"]
    assert "更正后的客户确认记录" not in mismatch["action"]
    service.close()


def test_phone_only_invite_waits_for_manual_forwarding(tmp_path):
    service = CollaborationService(tmp_path / "case.sqlite3", root=tmp_path)
    order = Order(order_id="SO-PHONE", customer="西区工地",
                  items=[{"sku": "水泥", "quantity": "10", "unit_price": "30"}],
                  source="manual/orders/SO-PHONE.json",
                  delivery_due_date=date.today() + timedelta(days=1),
                  allow_split_delivery=True)
    contacts = {role: {"name": role, "phone": "+8613800000000", "contact_allowed": True}
                for role in ("supplier", "driver", "receiver")}
    service.create_order(order, contacts)
    notices = service.notifications("SO-PHONE")
    assert len(notices) == 2
    assert all(n["channel"] == "manual" and n["status"] == "manual" for n in notices)
    assert service.dispatch_queued() == 0
    service.close()


def test_delivery_original_amount_and_attachment_are_auditable(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-PRICE", Decimal("10"),
                            declared_amount=Decimal("280"),
                            evidence=("delivery.pdf", b"original delivery document"))
    assert any(i["code"] == "AMOUNT_MISMATCH" for i in service.run_agent("SO-001")["issues"])
    files = service.evidence_files("SO-001")
    assert len(files) == 1 and files[0]["path"].read_bytes() == b"original delivery document"
    service.close()
