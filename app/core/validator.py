from collections import Counter, defaultdict
from decimal import Decimal
from datetime import date

from .matcher import match_order
from .models import AuditEvent, Batch, Issue


def validate_order(batch: Batch, order_id: str, *, as_of: date | None = None) -> tuple[list[Issue], list[AuditEvent]]:
    order, deliveries, confirmations = match_order(batch, order_id)
    issues: list[Issue] = []
    log = [AuditEvent(step="match_documents", detail=f"{order_id}：匹配到 {len(deliveries)} 张送货单")]

    def add(delivery_id: str | None, code: str, detail: str, sources: list[str], action: str) -> None:
        issue_id = f"{order_id}:{delivery_id or 'order'}:{code}"
        issues.append(Issue(issue_id=issue_id, order_id=order_id, delivery_id=delivery_id,
                            code=code, detail=detail, sources=sorted(set(sources)), action=action))
        log.append(AuditEvent(step="check_evidence", detail=f"{issue_id}: {detail}"))

    order_items = {item.sku: item for item in order.items}
    if not deliveries:
        add(None, "MISSING_DELIVERY", "尚未登记送货单，暂时无法核对实际送货",
            [order.source], "向仓库或配送人员取得送货单并登记")
        return issues, log
    seen_ids: dict[str, str] = {}
    delivered_total: dict[str, Decimal] = defaultdict(Decimal)
    for delivery in deliveries:
        if delivery.delivery_id in seen_ids:
            add(delivery.delivery_id, "DUPLICATE_DELIVERY", "送货单编号重复",
                [seen_ids[delivery.delivery_id], delivery.source], "核对重复单据并作废错误记录")
            continue  # Never double-count the repeated document.
        seen_ids[delivery.delivery_id] = delivery.source
        if delivery.customer != order.customer:
            add(delivery.delivery_id, "CUSTOMER_MISMATCH", "送货单客户与订单不一致",
                [order.source, delivery.source], "核对收货人身份")
        delivered = Counter()
        amount = Decimal("0")
        for item in delivery.items:
            delivered[item.sku] += item.quantity
            delivered_total[item.sku] += item.quantity
            amount += item.quantity * item.unit_price
            expected = order_items.get(item.sku)
            if expected is None:
                add(delivery.delivery_id, f"UNKNOWN_SKU:{item.sku}", f"商品 {item.sku} 不在订单中",
                    [order.source, delivery.source], "核对商品信息")
            elif item.unit_price != expected.unit_price:
                add(delivery.delivery_id, f"PRICE_MISMATCH:{item.sku}",
                    f"{item.sku}：订单单价 {expected.unit_price}，送货单单价 {item.unit_price}",
                    [order.source, delivery.source], "提供单价更正依据")
        if amount != delivery.declared_amount:
            add(delivery.delivery_id, "AMOUNT_MISMATCH",
                f"申报金额 {delivery.declared_amount}，明细计算金额 {amount} {order.currency}",
                [delivery.source], "更正送货单金额并说明原因")
        evidence = [c for c in confirmations[delivery.delivery_id] if c.evidence_ref]
        if not evidence:
            add(delivery.delivery_id, "MISSING_SIGNATURE", "缺少经人工审核的客户签收凭证",
                [delivery.source] + [c.source for c in confirmations[delivery.delivery_id]],
                "上传客户确认文件并完成人工审核")
        else:
            accepted = Counter()
            for confirmation in evidence:
                if confirmation.condition in {"partial", "damaged", "refused"}:
                    condition_label = {"partial": "部分接收", "damaged": "有损坏",
                                       "refused": "拒收"}[confirmation.condition]
                    add(delivery.delivery_id, f"RECEIPT_EXCEPTION:{confirmation.confirmation_id}",
                        f"收货人反馈：{condition_label}；{confirmation.note or '未说明原因'}",
                        [delivery.source, confirmation.source],
                        "供应商与收货方核实差异、损坏或拒收，并保存处理依据")
                for item in confirmation.items:
                    accepted[item.sku] += item.quantity
            for sku, qty in delivered.items():
                if accepted[sku] != qty:
                    add(delivery.delivery_id, f"ACCEPTED_QTY_MISMATCH:{sku}",
                        f"{sku}：送达 {qty}，客户确认 {accepted[sku]}",
                        [delivery.source] + [c.source for c in evidence],
                        "联系配送方与收货人核实差异；保留双方原始记录，并按事实补送、退货或更正责任方单据")
    for sku, item in order_items.items():
        if delivered_total[sku] != item.quantity:
            if (order.allow_split_delivery and order.delivery_due_date and
                    (as_of or date.today()) <= order.delivery_due_date and delivered_total[sku] < item.quantity):
                log.append(AuditEvent(step="await_planned_delivery",
                                      detail=f"{sku}：交付期限前允许分批，暂不认定少送"))
                continue
            add(None, f"ORDER_QTY_MISMATCH:{sku}",
                f"{sku}：订单要求 {item.quantity}，已送达 {delivered_total[sku]}",
                [order.source] + [d.source for d in deliveries],
                "补充送货记录或更正订单")
    return issues, log
