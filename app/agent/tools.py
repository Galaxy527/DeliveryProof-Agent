"""Business tools exposed to the model. All arguments are validated here."""
from app.core.validator import validate_order
from app.services.task_manager import TaskManager
from datetime import datetime, timezone


TOOL_SPECS = [
    {"type": "function", "function": {"name": name, "description": description,
      "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}},
                     "required": ["order_id"]}}}
    for name, description in (
        ("query_order", "查询订单及商品明细"),
        ("match_documents", "匹配送货单与客户确认记录"),
        ("check_evidence", "调用确定性规则核对数量、金额、重复单据和签收"),
        ("create_evidence_task", "为当前异常创建或更新补证任务"),
        ("list_open_tasks", "查询仍未解决的补证任务"),
        ("draft_followup_message", "根据仍未解决的缺签任务生成待人工确认的客户补证提醒草稿，不自动发送"),
        ("reverify_delivery", "补充新证据后重新核验订单"),
    )
]


class BusinessTools:
    def __init__(self, manager: TaskManager, order_id: str):
        self.manager = manager
        self.order_id = order_id
        self.log: list[dict] = []

    def execute(self, name: str, arguments: dict) -> dict:
        if name not in {spec["function"]["name"] for spec in TOOL_SPECS}:
            raise ValueError(f"Unknown tool {name}")
        if set(arguments) != {"order_id"} or arguments["order_id"] != self.order_id:
            raise ValueError("Tool may only access the selected order")
        batch = self.manager.get_batch(reviewed_only=True)
        if name == "query_order":
            matches = [o for o in batch.orders if o.order_id == self.order_id]
            result = matches[0].model_dump(mode="json") if len(matches) == 1 else {"error": "order not unique"}
        elif name == "match_documents":
            deliveries = [d for d in batch.deliveries if d.order_id == self.order_id]
            ids = {d.delivery_id for d in deliveries}
            result = {"deliveries": [d.model_dump(mode="json") for d in deliveries],
                      "confirmations": [c.model_dump(mode="json") for c in batch.confirmations
                                        if c.delivery_id in ids]}
        elif name == "check_evidence":
            issues, _ = validate_order(batch, self.order_id)
            result = {"issues": [i.model_dump(mode="json") for i in issues]}
        elif name in ("create_evidence_task", "reverify_delivery"):
            verification = self.manager.verify(None, self.order_id)
            result = {"status": verification.status,
                      "issues": [i.model_dump(mode="json") for i in verification.issues],
                      "tasks": [t.model_dump(mode="json") for t in verification.tasks]}
        elif name == "draft_followup_message":
            orders = [order for order in batch.orders if order.order_id == self.order_id]
            if len(orders) != 1:
                result = {"error": "找不到唯一订单"}
            else:
                signature_tasks = [task for task in self.manager.get_tasks(self.order_id)
                                   if task.status == "open" and ":MISSING_SIGNATURE" in task.issue_id]
                delivery_ids = sorted({task.issue_id.split(":")[1] for task in signature_tasks})
                message = (f"{orders[0].customer}，您好。我们正在核对订单 {self.order_id} 的交付资料，"
                           f"送货单 { '、'.join(delivery_ids) } 仍缺少可核验的收货确认。"
                           "请您核对实际收货情况并提供签收或明确的收货确认。谢谢。") if delivery_ids else None
                result = {"message": message, "requires_human_review": True,
                          "sent": False, "note": "草稿仅供人工核对与发送；系统没有联系客户。"}
        else:
            result = {"tasks": [t.model_dump(mode="json") for t in self.manager.get_tasks(self.order_id)
                                if t.status == "open"]}
        self.log.append({"tool": name, "arguments": arguments, "result": result,
                         "called_at": datetime.now(timezone.utc).isoformat()})
        return result
