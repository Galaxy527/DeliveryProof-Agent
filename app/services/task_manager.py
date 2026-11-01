"""SQLite evidence store, task lifecycle, and verification history."""
import json
import sqlite3
import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

from app.core.models import AuditEvent, Batch, Confirmation, Delivery, EvidenceTask, VerificationResult
from app.core.validator import validate_order


class TaskManager:
    def __init__(self, database: str | Path, upload_root: str | Path | None = None):
        self.upload_root = Path(upload_root or Path(__file__).resolve().parents[2] / "uploads").resolve()
        self.connection = sqlite3.connect(str(database))
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                kind TEXT NOT NULL, source TEXT NOT NULL, document_id TEXT NOT NULL,
                payload TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(kind, source));
            CREATE TABLE IF NOT EXISTS document_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL,
                action TEXT NOT NULL, old_payload TEXT, new_payload TEXT,
                actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence_reviews (
                source TEXT PRIMARY KEY, file_path TEXT NOT NULL, file_sha256 TEXT NOT NULL,
                actor TEXT NOT NULL, page INTEGER NOT NULL, note TEXT NOT NULL,
                reviewed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                created_at TEXT NOT NULL, stop_reason TEXT NOT NULL,
                summary TEXT NOT NULL, tool_log TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, order_id TEXT NOT NULL,
                status TEXT NOT NULL, action TEXT NOT NULL, resolution TEXT);
            CREATE TABLE IF NOT EXISTS verification_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL,
                created_at TEXT NOT NULL, result TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(documents)")}
        if "active" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            self.connection.commit()

    def _save_document(self, kind: str, document_id: str, source: str, payload: str) -> bool:
        existing = self.connection.execute(
            "SELECT payload, active FROM documents WHERE kind=? AND source=?", (kind, source)
        ).fetchone()
        if existing:
            if json.loads(existing[0]) != json.loads(payload):
                raise ValueError(f"Source already exists with different content: {source}")
            if not existing[1]:
                raise ValueError(f"Source was voided: {source}")
            return False
        self.connection.execute(
            "INSERT INTO documents(kind, source, document_id, payload) VALUES (?, ?, ?, ?)",
            (kind, source, document_id, payload))
        return True

    def import_batch(self, batch: Batch, *, trusted_demo: bool = False) -> int:
        if trusted_demo:
            canonical = Batch.model_validate_json(
                (Path(__file__).resolve().parents[2] / "data/samples/batch.json").read_text(encoding="utf-8"))
            if batch.model_dump(mode="json") != canonical.model_dump(mode="json"):
                raise ValueError("演示信任仅适用于项目内置的完整匿名样例")
        count = 0
        with self.connection:
            for kind, items, id_field in (
                ("order", batch.orders, "order_id"),
                ("delivery", batch.deliveries, "delivery_id"),
                ("confirmation", batch.confirmations, "confirmation_id"),
            ):
                for item in items:
                    count += self._save_document(kind, getattr(item, id_field), item.source,
                                                 item.model_dump_json())
                    if trusted_demo and kind == "confirmation" and item.evidence_ref:
                        self.connection.execute("""INSERT OR IGNORE INTO evidence_reviews
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (item.source, item.evidence_ref, "synthetic-demo", "演示样例",
                             1, "此记录仅用于匿名模拟演示", datetime.now(timezone.utc).isoformat()))
        return count

    def get_batch(self, *, reviewed_only: bool = False) -> Batch:
        grouped = {"order": [], "delivery": [], "confirmation": []}
        reviews = {}
        for source, file_path, expected_hash in self.connection.execute(
            "SELECT source, file_path, file_sha256 FROM evidence_reviews"
        ):
            if expected_hash == "synthetic-demo":
                reviews[source] = file_path
                continue
            path = (self.upload_root.parent / file_path).resolve()
            if (path.is_relative_to(self.upload_root) and path.is_file() and
                    hashlib.sha256(path.read_bytes()).hexdigest() == expected_hash):
                reviews[source] = file_path
        for kind, source, payload in self.connection.execute(
            "SELECT kind, source, payload FROM documents WHERE active=1 ORDER BY rowid"
        ):
            item = json.loads(payload)
            if reviewed_only and kind == "confirmation" and (source not in reviews or
                                                              item.get("evidence_ref") != reviews[source]):
                item["evidence_ref"] = None
            grouped[kind].append(item)
        return Batch(orders=grouped["order"], deliveries=grouped["delivery"],
                     confirmations=grouped["confirmation"])

    def verify(self, batch: Batch | None, order_id: str, *, as_of: date | None = None) -> VerificationResult:
        if batch is not None:
            self.import_batch(batch)
        issues, log = validate_order(self.get_batch(reviewed_only=True), order_id, as_of=as_of)
        active = {issue.issue_id for issue in issues}
        with self.connection:
            for issue in issues:
                row = self.connection.execute("SELECT status FROM tasks WHERE task_id=?",
                                              (issue.issue_id,)).fetchone()
                if row is None:
                    self.connection.execute("INSERT INTO tasks VALUES (?, ?, ?, 'open', ?, NULL)",
                                            (issue.issue_id, issue.issue_id, order_id, issue.action))
                    log.append(AuditEvent(step="create_evidence_task", detail=issue.issue_id))
                elif row[0] == "resolved":
                    self.connection.execute(
                        "UPDATE tasks SET status='open', resolution=NULL, action=? WHERE task_id=?",
                        (issue.action, issue.issue_id))
                    log.append(AuditEvent(step="reopen_evidence_task", detail=issue.issue_id))
            for (task_id,) in self.connection.execute(
                "SELECT task_id FROM tasks WHERE order_id=? AND status='open'", (order_id,)
            ).fetchall():
                if task_id not in active:
                    self.connection.execute(
                        "UPDATE tasks SET status='resolved', resolution='补充证据已复核' WHERE task_id=?",
                        (task_id,))
                    log.append(AuditEvent(step="resolve_evidence_task", detail=task_id))
            tasks = self.get_tasks(order_id)
            log.append(AuditEvent(step="archive_result", detail=f"{len(issues)} active issues"))
            result = VerificationResult(order_id=order_id,
                                        status="needs_evidence" if issues else "verified",
                                        issues=issues, tasks=tasks, log=log)
            self.connection.execute(
                "INSERT INTO verification_runs(order_id, created_at, result) VALUES (?, ?, ?)",
                (order_id, datetime.now(timezone.utc).isoformat(), result.model_dump_json()))
        return result

    def submit_evidence(self, *, delivery: Delivery | None = None,
                        confirmation: Confirmation | None = None) -> VerificationResult:
        if delivery is None and confirmation is None:
            raise ValueError("Provide a delivery or confirmation")
        current = self.get_batch()
        if delivery is not None:
            order_id = delivery.order_id
            if not any(o.order_id == order_id for o in current.orders):
                raise ValueError(f"Unknown order {order_id}")
        else:
            matches = [d for d in current.deliveries if d.delivery_id == confirmation.delivery_id]
            if len(matches) != 1:
                raise ValueError("Confirmation must match exactly one delivery")
            order_id = matches[0].order_id
        if confirmation is not None:
            if any(c.confirmation_id == confirmation.confirmation_id for c in current.confirmations):
                raise ValueError(f"Confirmation ID already submitted: {confirmation.confirmation_id}")
            matches = [d for d in current.deliveries if d.delivery_id == confirmation.delivery_id]
            if delivery is not None and delivery.delivery_id == confirmation.delivery_id:
                matches.append(delivery)
            if len(matches) != 1 or matches[0].order_id != order_id:
                raise ValueError("Confirmation must match exactly one delivery on the same order")
        with self.connection:
            if delivery is not None and not self._save_document(
                "delivery", delivery.delivery_id, delivery.source, delivery.model_dump_json()
            ):
                raise ValueError(f"Delivery source already submitted: {delivery.source}")
            if confirmation is not None and not self._save_document(
                "confirmation", confirmation.confirmation_id, confirmation.source,
                confirmation.model_dump_json()
            ):
                raise ValueError(f"Confirmation source already submitted: {confirmation.source}")
        return self.verify(None, order_id)

    def get_tasks(self, order_id: str) -> list[EvidenceTask]:
        return [EvidenceTask(task_id=row[0], issue_id=row[1], order_id=row[2], status=row[3],
                             action=row[4], resolution=row[5])
                for row in self.connection.execute(
                    "SELECT task_id, issue_id, order_id, status, action, resolution "
                    "FROM tasks WHERE order_id=? ORDER BY task_id", (order_id,))]

    def get_history(self, order_id: str) -> list[dict]:
        return [{"run_id": row[0], "created_at": row[1], "result": json.loads(row[2])}
                for row in self.connection.execute(
                    "SELECT run_id, created_at, result FROM verification_runs "
                    "WHERE order_id=? ORDER BY run_id", (order_id,))]

    def get_documents(self, order_id: str) -> list[dict]:
        return [{"kind": kind, "source": source, "document_id": doc_id,
                 "active": bool(active), "payload": json.loads(payload)}
                for kind, source, doc_id, active, payload in self.connection.execute(
                    "SELECT kind, source, document_id, active, payload FROM documents ORDER BY rowid")
                if (kind == "order" and doc_id == order_id) or
                (kind == "delivery" and json.loads(payload)["order_id"] == order_id) or
                (kind == "confirmation" and any(d.delivery_id == json.loads(payload)["delivery_id"]
                                                 for d in self.get_batch().deliveries
                                                 if d.order_id == order_id))]

    def get_document_events(self, source: str) -> list[dict]:
        return [{"action": row[0], "old_payload": json.loads(row[1]) if row[1] else None,
                 "new_payload": json.loads(row[2]) if row[2] else None, "actor": row[3],
                 "reason": row[4], "created_at": row[5]}
                for row in self.connection.execute(
                    "SELECT action, old_payload, new_payload, actor, reason, created_at "
                    "FROM document_events WHERE source=? ORDER BY event_id", (source,))]

    def void_document(self, kind: str, source: str, *, actor: str, reason: str) -> VerificationResult:
        if kind not in {"delivery", "confirmation"} or not actor.strip() or not reason.strip():
            raise ValueError("作废仅支持送货单或确认记录，且必须填写操作人和原因")
        row = self.connection.execute("SELECT payload FROM documents WHERE kind=? AND source=? AND active=1",
                                      (kind, source)).fetchone()
        if row is None:
            raise ValueError("找不到有效单据")
        item = json.loads(row[0])
        order_id = item["order_id"] if kind == "delivery" else self._order_for_delivery(item["delivery_id"])
        with self.connection:
            self.connection.execute("UPDATE documents SET active=0 WHERE kind=? AND source=?", (kind, source))
            self.connection.execute("DELETE FROM evidence_reviews WHERE source=?", (source,))
            self._document_event(kind, source, "void", row[0], None, actor, reason)
        return self.verify(None, order_id)

    def correct_document(self, kind: str, source: str, replacement, *, actor: str,
                         reason: str) -> VerificationResult:
        if kind not in {"order", "delivery", "confirmation"} or not actor.strip() or not reason.strip():
            raise ValueError("更正必须填写有效类型、操作人和原因")
        row = self.connection.execute("SELECT document_id, payload FROM documents "
                                      "WHERE kind=? AND source=? AND active=1", (kind, source)).fetchone()
        if row is None or replacement.source != source:
            raise ValueError("更正必须保留原单据来源")
        field = {"order": "order_id", "delivery": "delivery_id", "confirmation": "confirmation_id"}[kind]
        if getattr(replacement, field) != row[0]:
            raise ValueError("更正不可改变单据 ID")
        old = json.loads(row[1])
        new = replacement.model_dump_json()
        if kind == "order":
            order_id = row[0]
        elif kind == "delivery":
            order_id = old["order_id"]
            if replacement.order_id != order_id:
                raise ValueError("更正不可改变关联订单")
        else:
            order_id = self._order_for_delivery(old["delivery_id"])
            if replacement.delivery_id != old["delivery_id"]:
                raise ValueError("更正不可改变关联送货单")
        with self.connection:
            self.connection.execute("UPDATE documents SET payload=? WHERE kind=? AND source=?",
                                    (new, kind, source))
            if kind == "confirmation":
                self.connection.execute("DELETE FROM evidence_reviews WHERE source=?", (source,))
            self._document_event(kind, source, "correct", row[1], new, actor, reason)
        return self.verify(None, order_id)

    def _order_for_delivery(self, delivery_id: str) -> str:
        matches = {d.order_id for d in self.get_batch().deliveries if d.delivery_id == delivery_id}
        if len(matches) != 1:
            raise ValueError("关联送货单不唯一或不存在")
        return matches.pop()

    def _document_event(self, kind, source, action, old, new, actor, reason):
        self.connection.execute("INSERT INTO document_events(kind, source, action, old_payload, "
                                "new_payload, actor, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (kind, source, action, old, new, actor, reason,
                                 datetime.now(timezone.utc).isoformat()))

    def review_confirmation(self, source: str, file_path: str | Path, *, actor: str,
                            page: int, note: str) -> VerificationResult:
        root = self.upload_root.parent
        upload_root = self.upload_root
        path = Path(file_path).resolve()
        if not path.is_relative_to(upload_root) or not path.is_file():
            raise ValueError("审核文件必须位于项目 uploads 目录")
        if not actor.strip() or page < 1 or not note.strip():
            raise ValueError("请填写审核人、页码和审核依据")
        row = self.connection.execute("SELECT payload FROM documents WHERE kind='confirmation' "
                                      "AND source=? AND active=1", (source,)).fetchone()
        if row is None:
            raise ValueError("找不到有效客户确认记录")
        item = json.loads(row[0])
        if item.get("evidence_ref") != path.relative_to(root).as_posix():
            raise ValueError("确认记录的凭证引用与实际上传文件不一致")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.connection:
            self.connection.execute("""INSERT OR REPLACE INTO evidence_reviews
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, item["evidence_ref"], sha, actor.strip(), page, note.strip(),
                 datetime.now(timezone.utc).isoformat()))
        return self.verify(None, self._order_for_delivery(item["delivery_id"]))

    def get_reviews(self, order_id: str) -> list[dict]:
        sources = {c.source for c in self.get_batch().confirmations
                   if any(d.delivery_id == c.delivery_id for d in self.get_batch().deliveries
                          if d.order_id == order_id)}
        return [{"source": row[0], "file_path": row[1], "file_sha256": row[2],
                 "actor": row[3], "page": row[4], "note": row[5], "reviewed_at": row[6]}
                for row in self.connection.execute("SELECT * FROM evidence_reviews") if row[0] in sources]

    def save_agent_run(self, order_id: str, stop_reason: str, summary: str, tool_log: list[dict]) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO agent_runs(order_id, created_at, stop_reason, summary, tool_log) "
                "VALUES (?, ?, ?, ?, ?)",
                (order_id, datetime.now(timezone.utc).isoformat(), stop_reason,
                 summary, json.dumps(tool_log, ensure_ascii=False)))
        return cursor.lastrowid

    def get_agent_runs(self, order_id: str) -> list[dict]:
        return [{"run_id": row[0], "created_at": row[1], "stop_reason": row[2],
                 "summary": row[3], "tool_log": json.loads(row[4])}
                for row in self.connection.execute("SELECT run_id, created_at, stop_reason, summary, tool_log "
                                                   "FROM agent_runs WHERE order_id=? ORDER BY run_id", (order_id,))]

    def close(self) -> None:
        self.connection.close()
