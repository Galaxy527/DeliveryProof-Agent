"""A multi-party case must enforce scope and close only the issues evidenced."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.models import Order
from app.services.collaboration import CollaborationService
from app.agent.investigation import InvestigationAgent, InvestigationTools
import json


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


class ScenarioInvestigator:
    """Controllable stand-in for the model; tests tool routing without API cost."""

    sequence = ["get_case_snapshot", "list_evidence", "get_party_statements"]

    def chat(self, messages, tools):
        used = [m["tool_calls"][0]["function"]["name"] for m in messages
                if m.get("role") == "assistant" and m.get("tool_calls")]
        if len(used) < 3:
            name, args = self.sequence[len(used)], {}
        else:
            statements = json.loads(messages[-1]["content"])
            damaged = any("破损" in r["note"] for r in statements["receipts"])
            snapshot = json.loads(next(m["content"] for m in messages if m.get("role") == "tool"
                                       and "open_issues" in json.loads(m["content"])))
            issue = next(i for i in snapshot["open_issues"]
                         if i["code"].startswith("ACCEPTED_QTY_MISMATCH"))
            name = "propose_next_step"
            args = {"issue_id": issue["issue_id"],
                    "target_role": "driver" if damaged else "receiver",
                    "request": ("请提供卸货时的破损照片和退货数量记录，以核对短收原因。" if damaged else
                                "请核对实收数量并上传现场清点记录，说明少收五袋的情况。"),
                    "reason": ("收货人说明有破损，需核对卸货现场与退货去向。" if damaged else
                               "送货登记和实收数量不一致，当前缺少现场清点依据。"),
                    "evidence_refs": issue["sources"][:1]}
        return {"content": None, "tool_calls": [{"id": f"t-{len(used)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


@pytest.mark.parametrize("note,expected_role", [
    ("现场清点只收到 85 袋", "receiver"),
    ("现场收到 85 袋，另外 5 袋包装破损退回", "driver"),
])
def test_investigation_selects_next_party_from_evidence_and_waits_for_approval(
        tmp_path, note, expected_role):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("90"))
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("85"),
                           condition="partial", note=note)
    before = len(service.notifications("SO-001"))
    result = InvestigationAgent(service, ScenarioInvestigator()).run("SO-001")
    assert result["status"] == "draft"
    assert [e["tool"] for e in result["tool_log"]] == [
        "get_case_snapshot", "list_evidence", "get_party_statements", "propose_next_step"]
    assert service.investigation_runs("SO-001")[0]["draft_id"] == result["draft_id"]
    draft = service.investigations("SO-001")[0]
    assert draft["target_role"] == expected_role
    assert draft["status"] == "draft"
    assert len(service.notifications("SO-001")) == before
    service.approve_investigation("SO-001", draft["id"])
    with pytest.raises(PermissionError):
        service.respond_to_investigation(tokens["receiver" if expected_role == "driver" else "driver"],
                                         draft["id"], "这是其他人的回复")
    service.respond_to_investigation(tokens[expected_role], draft["id"],
                                     "已核对现场记录，附照片。", ("proof.pdf", b"synthetic evidence"))
    assert service.investigations("SO-001")[0]["status"] == "answered"
    assert service.investigation_responses("SO-001")[0]["attachment_valid"] is True
    assert len([n for n in service.notifications("SO-001") if n["role"] == expected_role]) > 0
    next_result = InvestigationAgent(service, ScenarioInvestigator()).run("SO-001")
    assert next_result["status"] == "draft"
    assert next_result["draft_id"] != draft["id"]
    service.close()


def test_supplier_can_answer_approved_investigation_without_sending_to_other_party(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("90"))
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("85"),
                           condition="partial", note="现场有五袋包装破损退回")
    issue = next(i for i in service.run_agent("SO-001")["issues"]
                 if i["code"].startswith("ACCEPTED_QTY_MISMATCH"))
    draft = service.save_investigation_draft("SO-001", issue_id=issue["issue_id"],
        target_role="supplier", request="请核对仓库是否收到五袋退货，并提交退货入库记录。",
        reason="收货人称有五袋破损退回，需要仓库记录确认去向。",
        evidence_refs=issue["sources"][:1])
    before = len(service.notifications("SO-001"))
    service.approve_investigation("SO-001", draft["draft_id"])
    assert len(service.notifications("SO-001")) == before
    with pytest.raises(PermissionError):
        service.respond_to_investigation(tokens["driver"], draft["draft_id"], "司机代答无效")
    service.supplier_respond_to_investigation("SO-001", draft["draft_id"],
        "仓库已收到五袋退货，附入库记录。", ("return.pdf", b"synthetic return record"))
    assert service.investigations("SO-001")[0]["status"] == "answered"
    assert service.investigation_responses("SO-001")[0]["role"] == "supplier"
    service.close()


def test_investigation_does_not_call_model_for_normal_case_or_existing_draft(tmp_path):
    class MustNotCall:
        def chat(self, messages, tools):
            raise AssertionError("正常订单或待审批草稿不应再次调用模型")

    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("100"))
    assert InvestigationAgent(service, MustNotCall()).run("SO-001")["model_calls"] == 0
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("95"),
                           condition="partial", note="现场只收到了九十五袋")
    first = InvestigationAgent(service, ScenarioInvestigator()).run("SO-001")
    second = InvestigationAgent(service, MustNotCall()).run("SO-001")
    assert second["draft_id"] == first["draft_id"]
    assert second["model_calls"] == 0
    service.close()


def test_investigation_rejects_unknown_evidence_and_stale_approval(tmp_path):
    service, tokens = case(tmp_path)
    service.record_delivery(tokens["driver"], "DN-001", Decimal("90"))
    service.record_receipt(tokens["receiver"], "DN-001", Decimal("85"),
                           condition="partial", note="现场只收到 85 袋")
    issue = next(i for i in service.run_agent("SO-001")["issues"]
                 if i["code"].startswith("ACCEPTED_QTY_MISMATCH"))
    kwargs = {"issue_id": issue["issue_id"], "target_role": "driver",
              "request": "请提交卸货时的照片和清点记录，核对短收数量。",
              "reason": "收货记录与司机送货数量相差五袋，需要卸货证据。",
              "evidence_refs": [issue["sources"][0]]}
    with pytest.raises(ValueError):
        service.save_investigation_draft("SO-001", **{**kwargs, "evidence_refs": ["other-order"]})
    tools = InvestigationTools(service, "SO-001")
    with pytest.raises(ValueError):
        tools.execute("propose_next_step", kwargs)
    first = service.save_investigation_draft("SO-001", **kwargs)
    same = service.save_investigation_draft("SO-001", **kwargs)
    assert first["draft_id"] == same["draft_id"] and same["reused"]
    service.record_delivery(tokens["driver"], "DN-002", Decimal("10"))
    assert service.investigations("SO-001")[0]["status"] == "stale"
    with pytest.raises(ValueError):
        service.approve_investigation("SO-001", first["draft_id"])
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
