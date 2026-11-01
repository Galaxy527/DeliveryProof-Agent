"""Role-scoped delivery cases and an event-driven follow-up agent.

Invitation links are bearer credentials. The raw token is returned once and only
its SHA-256 digest is stored. No external message is sent without a configured
transport and an HTTPS public URL.
"""
from __future__ import annotations

import hashlib
import hmac
import base64
import json
import os
import secrets
import smtplib
import ssl
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.models import Batch, Confirmation, Delivery, Order
from app.core.config import load_local_env
from app.services.task_manager import TaskManager


ROLES = {"driver": "配送人员", "receiver": "现场收货人"}
CONDITIONS = {"accepted": "全部接收", "partial": "部分接收", "damaged": "有损坏", "refused": "拒收"}
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CollaborationService:
    def __init__(self, database: str | Path, *, root: str | Path | None = None):
        self.root = Path(root or Path(__file__).resolve().parents[2]).resolve()
        load_local_env(self.root)
        secret_file = self.root / ".tmp" / "portal-secret"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if not secret_file.exists():
            secret_file.write_bytes(secrets.token_bytes(32))
        self._secret = secret_file.read_bytes()
        self.manager = TaskManager(database, upload_root=self.root / "uploads")
        self.db = self.manager.connection
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS case_participants (
                order_id TEXT NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL,
                email TEXT, phone TEXT, token_hash TEXT NOT NULL UNIQUE,
                token_nonce TEXT NOT NULL, contact_allowed INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(order_id, role));
            CREATE TABLE IF NOT EXISTS case_owners (
                order_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                email TEXT, phone TEXT, contact_allowed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                event_type TEXT NOT NULL, actor_role TEXT NOT NULL, detail TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                order_id TEXT NOT NULL, role TEXT NOT NULL, channel TEXT NOT NULL,
                recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL, provider_ref TEXT, created_at TEXT NOT NULL,
                sent_at TEXT);
            CREATE TABLE IF NOT EXISTS delivery_arrivals (
                delivery_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                arrived_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_receipts (
                delivery_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                accepted_quantity TEXT NOT NULL, condition TEXT NOT NULL,
                note TEXT NOT NULL, actor TEXT NOT NULL, submitted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_delivery_files (
                delivery_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, file_path TEXT NOT NULL,
                file_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_issue_epochs (
                issue_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                epoch INTEGER NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS case_resolutions (
                issue_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                explanation TEXT NOT NULL, evidence_path TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL, approved_by TEXT NOT NULL,
                approved_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_investigations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                issue_id TEXT NOT NULL, target_role TEXT NOT NULL,
                request TEXT NOT NULL, reason TEXT NOT NULL, evidence_refs TEXT NOT NULL,
                case_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, approved_at TEXT);
            CREATE TABLE IF NOT EXISTS case_investigation_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, investigation_id INTEGER NOT NULL,
                order_id TEXT NOT NULL, role TEXT NOT NULL, actor TEXT NOT NULL,
                note TEXT NOT NULL, evidence_path TEXT, evidence_sha256 TEXT,
                submitted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS case_investigation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                status TEXT NOT NULL, draft_id INTEGER, tool_log TEXT NOT NULL,
                model_calls INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                created_at TEXT NOT NULL);
        """)

    def close(self) -> None:
        self.manager.close()

    def _event(self, order_id: str, kind: str, role: str, detail: str, now: datetime | None = None) -> None:
        self.db.execute("INSERT INTO case_events(order_id,event_type,actor_role,detail,created_at) "
                        "VALUES(?,?,?,?,?)", (order_id, kind, role, detail, (now or utc_now()).isoformat()))
        self.db.commit()

    def orders(self) -> list[Order]:
        return self.manager.get_batch().orders

    def order(self, order_id: str) -> Order:
        found = [o for o in self.orders() if o.order_id == order_id]
        if len(found) != 1:
            raise ValueError("找不到这笔订单")
        return found[0]

    def create_order(self, order: Order, contacts: dict[str, dict], *,
                     source_file: tuple[str, bytes] | None = None) -> dict[str, str]:
        if any(existing.order_id == order.order_id for existing in self.orders()):
            raise ValueError("订单号已存在")
        if not order.delivery_due_date:
            raise ValueError("请填写约定交付日期")
        owner = contacts.get("supplier", {})
        if not owner.get("name", "").strip() or not (owner.get("email") or owner.get("phone")):
            raise ValueError("请填写供应商订单负责人姓名及联系方式")
        for role in ROLES:
            info = contacts.get(role, {})
            if not info.get("name", "").strip() or not (info.get("email") or info.get("phone")):
                raise ValueError(f"请填写{ROLES[role]}姓名及至少一种联系方式")
            if not info.get("contact_allowed"):
                raise ValueError(f"请确认{ROLES[role]}已获知该联系方式用于交付任务通知")
        if source_file:
            name, content = source_file
            if not content or len(content) > 12 * 1024 * 1024:
                raise ValueError("订单文件大小须在 1 字节至 12 MB 之间")
            if Path(name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("订单文件只支持 PDF 或图片")
            folder = self.root / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            safe_name = Path(name).name
            stored = folder / f"{hashlib.sha256(content).hexdigest()[:16]}-{safe_name}"
            stored.write_bytes(content)
            order = order.model_copy(update={"source": stored.relative_to(self.root).as_posix()})
        self.manager.import_batch(Batch(orders=[order], deliveries=[], confirmations=[]))
        self.db.execute("INSERT INTO case_owners VALUES(?,?,?,?,?)",
                        (order.order_id, owner["name"].strip(), owner.get("email", "").strip(),
                         owner.get("phone", "").strip(), int(owner.get("contact_allowed", False))))
        self.db.commit()
        self.manager.verify(None, order.order_id)
        self._event(order.order_id, "order_created", "supplier", "订单已登记")
        return {role: self.invite(order.order_id, role, **contacts[role]) for role in ROLES}

    def invite(self, order_id: str, role: str, name: str, email: str = "", phone: str = "",
               contact_allowed: bool = False, *, days: int = 30) -> str:
        self.order(order_id)
        if role not in ROLES:
            raise ValueError("无效角色")
        nonce = secrets.token_hex(16)
        token = self._derive_token(order_id, role, nonce)
        expires = utc_now() + timedelta(days=days)
        self.db.execute("""INSERT INTO case_participants
            (order_id,role,name,email,phone,token_hash,token_nonce,contact_allowed,expires_at,active)
            VALUES(?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(order_id,role) DO UPDATE SET name=excluded.name,
            email=excluded.email,phone=excluded.phone,token_hash=excluded.token_hash,
            token_nonce=excluded.token_nonce,contact_allowed=excluded.contact_allowed,
            expires_at=excluded.expires_at,active=1""",
            (order_id, role, name.strip(), email.strip(), phone.strip(), _hash(token), nonce,
             int(contact_allowed), expires.isoformat()))
        self.db.commit()
        self._event(order_id, "invite_created", "supplier", f"已为{ROLES[role]}创建专属入口")
        self._queue(order_id, role, f"{order_id}:{role}:invite:{nonce}",
                    f"交付任务邀请：{order_id}",
                    f"你被指定为订单 {order_id} 的{ROLES[role]}。请使用专属任务入口提交你负责的记录。",
                    utc_now())
        return token

    def participant(self, token: str) -> dict:
        if not token or len(token) > 200:
            raise PermissionError("无效的任务链接")
        row = self.db.execute("""SELECT order_id,role,name,email,phone,expires_at,active
            FROM case_participants WHERE token_hash=?""", (_hash(token),)).fetchone()
        if not row or not row[6] or utc_now() >= datetime.fromisoformat(row[5]):
            raise PermissionError("任务链接无效或已过期，请联系供应商重新邀请")
        return dict(zip(("order_id", "role", "name", "email", "phone", "expires_at"), row[:6]))

    def participants(self, order_id: str) -> list[dict]:
        return [dict(zip(("role", "name", "email", "phone", "contact_allowed", "expires_at", "active"), row))
                for row in self.db.execute("""SELECT role,name,email,phone,contact_allowed,expires_at,active
                    FROM case_participants WHERE order_id=? ORDER BY role""", (order_id,))]

    def deliveries(self, order_id: str) -> list[Delivery]:
        return [d for d in self.manager.get_batch().deliveries if d.order_id == order_id]

    def record_delivery(self, token: str, delivery_id: str, quantity: Decimal,
                        *, declared_amount: Decimal | None = None,
                        evidence: tuple[str, bytes] | None = None,
                        now: datetime | None = None) -> None:
        actor = self.participant(token)
        if actor["role"] != "driver":
            raise PermissionError("只有该订单的配送人员可以登记送货")
        order = self.order(actor["order_id"])
        if len(order.items) != 1:
            raise ValueError("当前协作入口只支持单商品订单")
        if not delivery_id.strip() or not delivery_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("请输入有效的原始送货单号")
        if quantity <= 0:
            raise ValueError("实送数量必须大于零")
        if declared_amount is not None and declared_amount < 0:
            raise ValueError("送货单金额不能小于零")
        if any(d.delivery_id == delivery_id for d in self.manager.get_batch().deliveries):
            raise ValueError("送货单号已被使用")
        item = order.items[0]
        delivery = Delivery(delivery_id=delivery_id, order_id=order.order_id,
                            customer=order.customer,
                            items=[{"sku": item.sku, "quantity": quantity,
                                    "unit_price": item.unit_price}],
                            declared_amount=declared_amount if declared_amount is not None else quantity * item.unit_price,
                            source=f"portal/deliveries/{delivery_id}.json")
        if evidence:
            filename, content = evidence
            if not content or len(content) > 12 * 1024 * 1024 or Path(filename).suffix.lower() not in {
                    ".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("送货单附件须为 12 MB 以内的 PDF 或图片")
        self.manager.submit_evidence(delivery=delivery)
        if evidence:
            folder = self.root / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"delivery-{secrets.token_hex(12)}-{Path(filename).name}"
            path.write_bytes(content)
            self.db.execute("INSERT INTO case_delivery_files VALUES(?,?,?,?)",
                            (delivery_id, order.order_id, path.relative_to(self.root).as_posix(),
                             hashlib.sha256(content).hexdigest()))
        timestamp = (now or utc_now()).isoformat()
        self.db.execute("INSERT INTO delivery_arrivals VALUES(?,?,?)",
                        (delivery_id, order.order_id, timestamp))
        self.db.commit()
        self._event(order.order_id, "delivery_arrived", "driver",
                    f"送货单 {delivery_id}，实送 {quantity} {item.sku}", now)
        self.run_agent(order.order_id, now=now)

    def record_receipt(self, token: str, delivery_id: str, accepted: Decimal,
                       *, condition: str, note: str = "", evidence: tuple[str, bytes] | None = None,
                       now: datetime | None = None) -> None:
        actor = self.participant(token)
        if actor["role"] != "receiver":
            raise PermissionError("只有该订单的指定收货人可以确认收货")
        matches = [d for d in self.deliveries(actor["order_id"]) if d.delivery_id == delivery_id]
        if len(matches) != 1:
            raise PermissionError("这张送货单不属于你的订单")
        delivery = matches[0]
        if condition not in {"accepted", "partial", "refused", "damaged"}:
            raise ValueError("请选择实际收货情况")
        if accepted < 0 or accepted > delivery.items[0].quantity:
            raise ValueError("实收数量不能小于零或超过送货单数量")
        if condition == "accepted" and accepted != delivery.items[0].quantity:
            raise ValueError("全部接收时实收数量应等于送货单数量")
        if condition == "refused" and accepted != 0:
            raise ValueError("拒收时实收数量应为零")
        if condition in {"partial", "damaged", "refused"} and not note.strip():
            raise ValueError("请填写差异或拒收原因")
        if self.db.execute("SELECT 1 FROM delivery_receipts WHERE delivery_id=?", (delivery_id,)).fetchone():
            raise ValueError("该送货单已有收货确认，请联系供应商处理更正")
        if evidence:
            filename, content = evidence
            if not content or len(content) > 12 * 1024 * 1024 or Path(filename).suffix.lower() not in {
                    ".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("签收附件须为 12 MB 以内的 PDF 或图片")
        timestamp = now or utc_now()
        folder = self.root / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        if evidence:
            filename, content = evidence
            evidence_path = folder / f"receipt-{secrets.token_hex(12)}-{Path(filename).name}"
            evidence_path.write_bytes(content)
        ref = evidence_path.relative_to(self.root).as_posix() if evidence else None
        confirmation_id = "CONF-" + secrets.token_hex(8)
        confirmation = Confirmation(
            confirmation_id=confirmation_id, delivery_id=delivery_id,
            items=[{"sku": delivery.items[0].sku, "quantity": accepted}],
            evidence_ref=ref, source=f"portal/confirmations/{confirmation_id}.json",
            condition=condition, note=note[:1000])
        self.manager.submit_evidence(confirmation=confirmation)
        self.db.execute("INSERT INTO delivery_receipts VALUES(?,?,?,?,?,?,?)",
                        (delivery_id, actor["order_id"], str(accepted), condition, note[:1000],
                         actor["name"], timestamp.isoformat()))
        self.db.commit()
        self._event(actor["order_id"], "receipt_submitted", "receiver",
                    f"{delivery_id}：{CONDITIONS[condition]}，实收 {accepted}；{note[:300]}", timestamp)
        self.run_agent(actor["order_id"], now=now)

    def review_receipt(self, order_id: str, delivery_id: str, filename: str,
                       content: bytes, *, note: str) -> None:
        """Supplier records a separate review of the signed document."""
        self.order(order_id)
        if not note.strip() or len(content) == 0 or len(content) > 12 * 1024 * 1024:
            raise ValueError("请填写审核依据，上传 12 MB 以内的凭证")
        if Path(filename).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("凭证须为 PDF 或图片")
        docs = [d for d in self.manager.get_documents(order_id)
                if d["kind"] == "confirmation" and d["active"] and
                d["payload"]["delivery_id"] == delivery_id]
        if len(docs) != 1:
            raise ValueError("找不到唯一有效的收货反馈")
        source = docs[0]["source"]
        folder = self.root / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"review-{secrets.token_hex(12)}-{Path(filename).name}"
        path.write_bytes(content)
        confirmation = Confirmation.model_validate(docs[0]["payload"])
        replacement = confirmation.model_copy(update={"evidence_ref": path.relative_to(self.root).as_posix()})
        self.manager.correct_document("confirmation", source, replacement,
                                      actor="供应商负责人", reason="补充待审核签收凭证")
        self.manager.review_confirmation(source, path, actor="供应商负责人", page=1,
                                         note=note.strip())
        self._event(order_id, "receipt_reviewed", "supplier",
                    f"已审核送货单 {delivery_id} 的签收凭证：{note.strip()[:200]}")
        self.run_agent(order_id)

    def resolutions(self, order_id: str) -> list[dict]:
        rows = self.db.execute("SELECT issue_id,explanation,evidence_path,evidence_sha256,approved_by,approved_at "
                               "FROM case_resolutions WHERE order_id=?", (order_id,))
        valid = []
        for issue_id, explanation, ref, expected, actor, approved_at in rows:
            path = (self.root / ref).resolve()
            if (path.is_relative_to((self.root / "uploads").resolve()) and path.is_file() and
                    hashlib.sha256(path.read_bytes()).hexdigest() == expected):
                valid.append({"issue_id": issue_id, "explanation": explanation,
                              "evidence_path": ref, "approved_by": actor, "approved_at": approved_at})
        return valid

    def evidence_files(self, order_id: str) -> list[dict]:
        """Files visible only in the authenticated supplier view."""
        self.order(order_id)
        candidates = []
        order = self.order(order_id)
        candidates.append(("原始订单", order.source))
        candidates.extend((f"送货单 {delivery_id}", ref) for delivery_id, ref in self.db.execute(
            "SELECT delivery_id,file_path FROM case_delivery_files WHERE order_id=?", (order_id,)))
        candidates.extend((f"收货凭证 {d['document_id']}", d["payload"].get("evidence_ref"))
                          for d in self.manager.get_documents(order_id)
                          if d["kind"] == "confirmation" and d["active"])
        candidates.extend((f"差异处理 {r['issue_id']}", r["evidence_path"])
                          for r in self.resolutions(order_id))
        candidates.extend((f"调查回复 {r['ref']}", r["evidence_ref"])
                          for r in self.investigation_responses(order_id))
        found = []
        upload_root = (self.root / "uploads").resolve()
        for label, ref in candidates:
            if not ref:
                continue
            path = (self.root / ref).resolve()
            if path.is_relative_to(upload_root) and path.is_file():
                found.append({"label": label, "ref": ref, "path": path, "filename": path.name})
        return found

    def investigation_responses(self, order_id: str) -> list[dict]:
        self.order(order_id)
        rows = self.db.execute("""SELECT id,investigation_id,role,actor,note,evidence_path,
            evidence_sha256,submitted_at FROM case_investigation_responses
            WHERE order_id=? ORDER BY id""", (order_id,)).fetchall()
        result = []
        for row in rows:
            response_id, draft_id, role, actor, note, ref, expected, submitted = row
            path = (self.root / ref).resolve() if ref else None
            valid = bool(path and path.is_relative_to((self.root / "uploads").resolve()) and
                         path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
            result.append({"ref": f"response:{response_id}", "investigation_id": draft_id,
                           "role": role, "actor": actor, "note": note,
                           "evidence_ref": ref if valid else None,
                           "attachment_valid": valid if ref else None,
                           "submitted_at": submitted})
        return result

    def _investigation_fingerprint(self, order_id: str) -> str:
        docs = [(d["source"], d["active"], d["payload"]) for d in
                self.manager.get_documents(order_id)]
        responses = self.investigation_responses(order_id)
        material = json.dumps({"documents": docs, "responses": responses,
                               "resolutions": self.resolutions(order_id)},
                              sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def investigations(self, order_id: str) -> list[dict]:
        self.order(order_id)
        fingerprint = self._investigation_fingerprint(order_id)
        rows = self.db.execute("""SELECT id,issue_id,target_role,request,reason,evidence_refs,
            case_fingerprint,status,created_at,approved_at FROM case_investigations
            WHERE order_id=? ORDER BY id DESC""", (order_id,)).fetchall()
        result = []
        for row in rows:
            draft_id, issue_id, role, request, reason, refs, saved_fingerprint, status, created, approved = row
            if status == "draft" and saved_fingerprint != fingerprint:
                status = "stale"
            result.append({"id": draft_id, "issue_id": issue_id, "target_role": role,
                           "request": request, "reason": reason,
                           "evidence_refs": json.loads(refs), "status": status,
                           "created_at": created, "approved_at": approved})
        return result

    def save_investigation_run(self, order_id: str, outcome: dict) -> None:
        self.order(order_id)
        self.db.execute("""INSERT INTO case_investigation_runs
            (order_id,status,draft_id,tool_log,model_calls,total_tokens,created_at)
            VALUES(?,?,?,?,?,?,?)""", (order_id, outcome["stop_reason"], outcome.get("draft_id"),
                                    json.dumps(outcome["tool_log"], ensure_ascii=False),
                                    outcome["model_calls"], outcome["total_tokens"], utc_now().isoformat()))
        self.db.commit()

    def investigation_runs(self, order_id: str) -> list[dict]:
        self.order(order_id)
        rows = self.db.execute("""SELECT id,status,draft_id,tool_log,model_calls,total_tokens,created_at
            FROM case_investigation_runs WHERE order_id=? ORDER BY id DESC""", (order_id,))
        return [{"id": row[0], "status": row[1], "draft_id": row[2],
                 "tool_log": json.loads(row[3]), "model_calls": row[4],
                 "total_tokens": row[5], "created_at": row[6]} for row in rows]

    def save_investigation_draft(self, order_id: str, *, issue_id: str, target_role: str,
                                 request: str, reason: str, evidence_refs: list[str]) -> dict:
        """Called by the model tool. It cannot send messages or settle a dispute."""
        self.order(order_id)
        issues = {i.issue_id: i for i in self.manager.verify(None, order_id).issues}
        issue = issues.get(issue_id)
        if not issue or not issue.code.startswith(("ACCEPTED_QTY_MISMATCH", "RECEIPT_EXCEPTION",
                                                  "ORDER_QTY_MISMATCH")):
            raise ValueError("只能调查当前未解决的数量或收货差异")
        if issue_id in {r["issue_id"] for r in self.resolutions(order_id)}:
            raise ValueError("该差异已有处理依据")
        if target_role not in {"driver", "receiver", "supplier"}:
            raise ValueError("无效的调查对象")
        if not isinstance(request, str) or not isinstance(reason, str) or not (
                12 <= len(request.strip()) <= 500 and 12 <= len(reason.strip()) <= 500):
            raise ValueError("补证请求与原因须具体、简短")
        if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 8:
            raise ValueError("须引用已查询到的证据")
        allowed = {d["source"] for d in self.manager.get_documents(order_id)}
        allowed.update(f["ref"] for f in self.evidence_files(order_id))
        allowed.update(r["ref"] for r in self.investigation_responses(order_id))
        allowed.update(issue.sources)
        if any(not isinstance(ref, str) or ref not in allowed for ref in evidence_refs):
            raise ValueError("证据引用不属于当前订单")
        fingerprint = self._investigation_fingerprint(order_id)
        existing = self.db.execute("""SELECT id FROM case_investigations WHERE order_id=?
            AND issue_id=? AND case_fingerprint=? AND status IN ('draft','approved')
            ORDER BY id DESC LIMIT 1""", (order_id, issue_id, fingerprint)).fetchone()
        if existing:
            return {"draft_id": existing[0], "sent": False, "reused": True}
        cursor = self.db.execute("""INSERT INTO case_investigations
            (order_id,issue_id,target_role,request,reason,evidence_refs,case_fingerprint,status,created_at)
            VALUES(?,?,?,?,?,?,?,'draft',?)""",
            (order_id, issue_id, target_role, request.strip(), reason.strip(),
             json.dumps(evidence_refs, ensure_ascii=False), fingerprint, utc_now().isoformat()))
        self.db.commit()
        self._event(order_id, "investigation_drafted", "agent",
                    f"调查草稿 {cursor.lastrowid}：建议向{ROLES.get(target_role, '供应商')}补证")
        return {"draft_id": cursor.lastrowid, "sent": False, "reused": False}

    def approve_investigation(self, order_id: str, draft_id: int) -> None:
        """Supplier explicitly releases a scoped request to the named participant."""
        draft = next((d for d in self.investigations(order_id) if d["id"] == draft_id), None)
        if not draft or draft["status"] != "draft":
            raise ValueError("调查草稿已失效或已经批准，请重新调查")
        active = {i.issue_id for i in self.manager.verify(None, order_id).issues}
        if draft["issue_id"] not in active:
            raise ValueError("原始差异已消失，请重新调查")
        now = utc_now()
        self.db.execute("UPDATE case_investigations SET status='approved',approved_at=? WHERE id=?",
                        (now.isoformat(), draft_id))
        self.db.commit()
        if draft["target_role"] != "supplier":
            self._queue(order_id, draft["target_role"], f"investigation:{draft_id}",
                        f"订单 {order_id} 的补证请求", draft["request"], now)
        self._event(order_id, "investigation_approved", "supplier",
                    f"已批准调查请求 {draft_id}；对象：{draft['target_role']}")

    def respond_to_investigation(self, token: str, draft_id: int, note: str,
                                 evidence: tuple[str, bytes] | None = None) -> None:
        actor = self.participant(token)
        self._record_investigation_response(actor["order_id"], draft_id, actor["role"],
                                            actor["name"], note, evidence)

    def supplier_respond_to_investigation(self, order_id: str, draft_id: int, note: str,
                                          evidence: tuple[str, bytes] | None = None) -> None:
        """Called only from the supplier-authenticated owner page."""
        self.order(order_id)
        self._record_investigation_response(order_id, draft_id, "supplier", "供应商负责人",
                                            note, evidence)

    def _record_investigation_response(self, order_id: str, draft_id: int, role: str,
                                       actor_name: str, note: str,
                                       evidence: tuple[str, bytes] | None) -> None:
        draft = next((d for d in self.investigations(order_id) if d["id"] == draft_id), None)
        if not draft or draft["status"] != "approved" or draft["target_role"] != role:
            raise PermissionError("这项补证请求不属于你的待办")
        active = {i.issue_id for i in self.manager.verify(None, order_id).issues}
        if draft["issue_id"] not in active or draft["issue_id"] in {
                r["issue_id"] for r in self.resolutions(order_id)}:
            raise ValueError("原始差异已处理，请重新调查")
        if len(note.strip()) < 5 or len(note) > 1000:
            raise ValueError("请填写 5 至 1000 字的情况说明")
        ref = expected = None
        if evidence:
            name, content = evidence
            if not content or len(content) > 12 * 1024 * 1024 or Path(name).suffix.lower() not in {
                    ".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError("补证附件须为 12 MB 以内的 PDF 或图片")
            folder = self.root / "uploads"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"followup-{secrets.token_hex(12)}-{Path(name).name}"
            path.write_bytes(content)
            ref = path.relative_to(self.root).as_posix()
            expected = hashlib.sha256(content).hexdigest()
        self.db.execute("""INSERT INTO case_investigation_responses
            (investigation_id,order_id,role,actor,note,evidence_path,evidence_sha256,submitted_at)
            VALUES(?,?,?,?,?,?,?,?)""", (draft_id, order_id, role, actor_name,
                                        note.strip(), ref, expected, utc_now().isoformat()))
        self.db.execute("UPDATE case_investigations SET status='answered' WHERE id=?", (draft_id,))
        self.db.commit()
        self._event(order_id, "investigation_answered", role,
                    f"补证请求 {draft_id} 已回复，待供应商复查")
        self.run_agent(order_id)

    def resolve_issue(self, order_id: str, issue_id: str, explanation: str,
                      filename: str, content: bytes) -> None:
        """Close a business exception with evidence; retain the rule finding."""
        self.order(order_id)
        issues = {i.issue_id: i for i in self.manager.verify(None, order_id).issues}
        issue = issues.get(issue_id)
        if issue is None or not issue.code.startswith(("RECEIPT_EXCEPTION", "ACCEPTED_QTY_MISMATCH",
                                                        "ORDER_QTY_MISMATCH")):
            raise ValueError("只能处理当前数量或收货差异；缺凭证或缺送货记录须先补齐")
        if len(explanation.strip()) < 10 or not content or len(content) > 12 * 1024 * 1024:
            raise ValueError("处理说明至少 10 字，并上传 12 MB 以内的处理依据")
        if Path(filename).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("处理依据须为 PDF 或图片")
        folder = self.root / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"resolution-{secrets.token_hex(12)}-{Path(filename).name}"
        path.write_bytes(content)
        self.db.execute("INSERT OR REPLACE INTO case_resolutions VALUES(?,?,?,?,?,?,?)",
                        (issue_id, order_id, explanation.strip(), path.relative_to(self.root).as_posix(),
                         hashlib.sha256(content).hexdigest(), "供应商负责人", utc_now().isoformat()))
        self.db.commit()
        self._event(order_id, "issue_resolved", "supplier", f"{issue_id}：{explanation.strip()[:200]}")
        self.run_agent(order_id)

    def receipts(self, order_id: str) -> list[dict]:
        return [dict(zip(("delivery_id", "accepted_quantity", "condition", "note",
                          "actor", "submitted_at"), row))
                for row in self.db.execute("""SELECT delivery_id,accepted_quantity,condition,
                    note,actor,submitted_at FROM delivery_receipts WHERE order_id=?""", (order_id,))]

    def events(self, order_id: str) -> list[dict]:
        return [dict(zip(("event_type", "actor_role", "detail", "created_at"), row))
                for row in self.db.execute("""SELECT event_type,actor_role,detail,created_at
                    FROM case_events WHERE order_id=? ORDER BY id DESC""", (order_id,))]

    def notifications(self, order_id: str) -> list[dict]:
        return [dict(zip(("id", "role", "channel", "recipient", "subject", "body",
                          "status", "provider_ref", "created_at", "sent_at"), row))
                for row in self.db.execute("""SELECT id,role,channel,recipient,subject,body,
                    status,provider_ref,created_at,sent_at FROM case_notifications
                    WHERE order_id=? ORDER BY id DESC""", (order_id,))]

    def _derive_token(self, order_id: str, role: str, nonce: str) -> str:
        digest = hmac.new(self._secret, f"{order_id}:{role}:{nonce}".encode(),
                          hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def task_link(self, order_id: str, role: str, *, local: bool = False) -> str | None:
        row = self.db.execute("SELECT token_nonce,active FROM case_participants "
                              "WHERE order_id=? AND role=?", (order_id, role)).fetchone()
        if not row or not row[1]:
            return None
        base = os.getenv("DELIVERYPROOF_PUBLIC_URL", "").rstrip("/")
        if not base and local:
            base = "http://127.0.0.1:8501"
        if not base:
            return None
        if not local and not base.startswith("https://"):
            return None
        return f"{base}/?token={self._derive_token(order_id, role, row[0])}"

    def _queue(self, order_id: str, role: str, event_key: str, subject: str,
               body: str, now: datetime, *, channel_override: str | None = None) -> None:
        if role == "supplier":
            row = self.db.execute("SELECT email,phone,contact_allowed FROM case_owners WHERE order_id=?",
                                  (order_id,)).fetchone()
        else:
            row = self.db.execute("SELECT email,phone,contact_allowed FROM case_participants WHERE order_id=? "
                                  "AND role=? AND active=1", (order_id, role)).fetchone()
        if not row or not row[2]:
            return
        email, phone, _ = row
        channel = channel_override or ("email" if email else "manual")
        recipient = email if channel == "email" else phone
        if not recipient:
            return
        status = "manual" if channel == "manual" else "queued"
        self.db.execute("""INSERT OR IGNORE INTO case_notifications
            (event_key,order_id,role,channel,recipient,subject,body,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (event_key, order_id, role, channel, recipient, subject, body, status, now.isoformat()))
        self.db.commit()

    def run_agent(self, order_id: str, *, now: datetime | None = None) -> dict:
        """Reverify and route due work; safe to call on every event or worker tick."""
        now = now or utc_now()
        order = self.order(order_id)
        business_date = now.astimezone(BUSINESS_TZ).date()
        result = self.manager.verify(None, order_id, as_of=business_date)
        issue_codes = {issue.code for issue in result.issues}
        closed_issue_ids = {row["issue_id"] for row in self.resolutions(order_id)}
        active_issue_ids = {issue.issue_id for issue in result.issues
                            if issue.issue_id not in closed_issue_ids}
        open_issue_ids = active_issue_ids
        epochs = {}
        for issue in result.issues:
            if issue.issue_id in closed_issue_ids:
                continue
            row = self.db.execute("SELECT epoch,active FROM case_issue_epochs WHERE issue_id=?",
                                  (issue.issue_id,)).fetchone()
            if row is None:
                epoch = 1
                self.db.execute("INSERT INTO case_issue_epochs VALUES(?,?,?,1)",
                                (issue.issue_id, order_id, epoch))
            elif not row[1]:
                epoch = row[0] + 1
                self.db.execute("UPDATE case_issue_epochs SET epoch=?,active=1 WHERE issue_id=?",
                                (epoch, issue.issue_id))
            else:
                epoch = row[0]
            epochs[issue.issue_id] = epoch
        for (old_id,) in self.db.execute("SELECT issue_id FROM case_issue_epochs "
                                         "WHERE order_id=? AND active=1", (order_id,)).fetchall():
            if old_id not in active_issue_ids:
                self.db.execute("UPDATE case_issue_epochs SET active=0 WHERE issue_id=?", (old_id,))
        for notification_id, event_key in self.db.execute(
                "SELECT id,event_key FROM case_notifications WHERE order_id=? AND status IN ('queued','manual')",
                (order_id,)).fetchall():
            if (":invite:" not in event_key and
                    not any(event_key.startswith(issue_id + f":cycle:{epochs[issue_id]}")
                            for issue_id in open_issue_ids)):
                self.db.execute("UPDATE case_notifications SET status='canceled' WHERE id=?",
                                (notification_id,))
        self.db.commit()
        for issue in result.issues:
            if issue.issue_id in closed_issue_ids:
                continue
            role = None
            if issue.code == "MISSING_SIGNATURE" and issue.delivery_id:
                receipt = self.db.execute("SELECT 1 FROM delivery_receipts WHERE delivery_id=?",
                                          (issue.delivery_id,)).fetchone()
                if receipt:
                    role = "supplier"
                else:
                    row = self.db.execute("SELECT arrived_at FROM delivery_arrivals WHERE delivery_id=?",
                                          (issue.delivery_id,)).fetchone()
                    if row and now - datetime.fromisoformat(row[0]) >= timedelta(hours=24):
                        role = "receiver"
            elif issue.code == "MISSING_DELIVERY" or issue.code.startswith("ORDER_QTY_MISMATCH"):
                if order.delivery_due_date and business_date > order.delivery_due_date:
                    role = "driver"
            elif issue.code.startswith("ACCEPTED_QTY_MISMATCH"):
                role = "driver"
            elif issue.code.startswith("RECEIPT_EXCEPTION"):
                role = "supplier"
            if role:
                event_key = issue.issue_id + f":cycle:{epochs[issue.issue_id]}"
                self._queue(order_id, role, event_key,
                            f"交付任务待处理：{order_id}",
                            f"订单 {order_id}：{issue.detail.rstrip('。')}。请使用你的专属任务入口核实并提交材料；如有争议请联系供应商管理员。",
                            now)
                first = self.db.execute("SELECT created_at FROM case_notifications WHERE event_key=?",
                                        (event_key,)).fetchone()
                if first and now - datetime.fromisoformat(first[0]) >= timedelta(hours=72):
                    self._queue(order_id, role, event_key + ":voice",
                                f"交付任务超时：{order_id}",
                                "交付任务超过 72 小时仍未处理，请联系供应商管理员。", now,
                                channel_override="phone")
        self._event(order_id, "agent_checked", "agent",
                    f"复核完成：{len(issue_codes)} 类问题，{len(result.issues)} 项待办", now)
        return result.model_dump(mode="json")

    def tick_all(self, *, now: datetime | None = None) -> int:
        now = now or utc_now()
        checked = 0
        for order in self.orders():
            row = self.db.execute("SELECT created_at FROM case_events WHERE order_id=? "
                                  "AND event_type='agent_checked' ORDER BY id DESC LIMIT 1",
                                  (order.order_id,)).fetchone()
            if row and now - datetime.fromisoformat(row[0]) < timedelta(hours=1):
                continue
            self.run_agent(order.order_id, now=now)
            checked += 1
        return checked

    def dispatch_queued(self) -> int:
        """Send configured email/voice notifications; never calls by default."""
        count = 0
        for row in self.db.execute("""SELECT id,order_id,role,channel,recipient,subject,body
            FROM case_notifications WHERE status='queued' ORDER BY id""").fetchall():
            notification_id, order_id, role, channel, recipient, subject, body = row
            try:
                if channel == "email" and os.getenv("DELIVERYPROOF_AUTO_EMAIL") == "1":
                    if not os.getenv("DELIVERYPROOF_PUBLIC_URL", "").startswith("https://"):
                        continue
                    link = self.task_link(order_id, role)
                    if link and link not in body:
                        body += f"\n专属任务入口：{link}"
                    self._send_email(recipient, subject, body)
                elif channel == "phone" and os.getenv("DELIVERYPROOF_AUTO_CALL") == "1":
                    self._call_phone(recipient)
                else:
                    continue
            except Exception as error:
                self.db.execute("UPDATE case_notifications SET status='failed',provider_ref=? WHERE id=?",
                                (type(error).__name__, notification_id))
            else:
                self.db.execute("UPDATE case_notifications SET status='sent',sent_at=? WHERE id=?",
                                (utc_now().isoformat(), notification_id))
                count += 1
            self.db.commit()
        return count

    @staticmethod
    def _send_email(recipient: str, subject: str, body: str) -> None:
        host = os.environ["DELIVERYPROOF_SMTP_HOST"]
        sender = os.environ["DELIVERYPROOF_SMTP_FROM"]
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = sender, recipient, subject
        message.set_content(body)
        with smtplib.SMTP_SSL(host, int(os.getenv("DELIVERYPROOF_SMTP_PORT", "465")),
                             context=ssl.create_default_context(), timeout=15) as server:
            server.login(os.environ["DELIVERYPROOF_SMTP_USER"],
                         os.environ["DELIVERYPROOF_SMTP_PASSWORD"])
            server.send_message(message)

    @staticmethod
    def _call_phone(recipient: str) -> None:
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        secret = os.environ["TWILIO_AUTH_TOKEN"]
        caller = os.environ["TWILIO_FROM_NUMBER"]
        form = urllib.parse.urlencode({
            "To": recipient, "From": caller,
            "Twiml": "<Response><Say language=\"zh-CN\">您好，您有一项交付资料待处理。请联系供应商管理员查看任务。谢谢。</Say></Response>"
        }).encode("utf-8")
        credential = base64.b64encode(f"{sid}:{secret}".encode()).decode()
        request = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", data=form,
            headers={"Authorization": "Basic " + credential,
                     "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status not in (200, 201):
                raise RuntimeError("语音服务未接受请求")
