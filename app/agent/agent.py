"""Bounded model-selected tool loop with an auditable deterministic result."""
import json
import re
from datetime import datetime, timezone

from app.services.task_manager import TaskManager
from .tools import BusinessTools, TOOL_SPECS


SYSTEM = """你是建材供应商的交付凭证核验 Agent。请选择工具查询订单、匹配单据、检查异常，必要时创建补证任务。若仍有缺少客户签收的任务，请调用 draft_followup_message 生成待人工核对的补证提醒草稿。
数量与金额只以工具中的确定性规则为准，不得自行臆测签名、证据或核验结论。所有工具只可操作当前订单。
不得声称已经联系客户或发送消息。最终回复必须使用简体中文，说明发现的异常、已执行的动作和下一步待办；不要输出英文业务说明。"""


class DeliveryAgent:
    def __init__(self, manager: TaskManager, client, max_steps: int = 8):
        self.manager = manager
        self.client = client
        self.max_steps = max_steps

    def run(self, order_id: str) -> dict:
        tools = BusinessTools(self.manager, order_id)
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"请核验订单 {order_id}，按需调用工具并处理补证任务。"}]
        final_text = ""
        stop_reason = "step_limit"
        for _ in range(self.max_steps):
            try:
                message = self.client.chat(messages, TOOL_SPECS)
            except Exception as error:
                safe_http = re.search(r"HTTP\s+(\d{3})", str(error))
                stop_reason = (f"model_error:HTTP_{safe_http.group(1)}" if safe_http else
                               f"model_error:{type(error).__name__}")
                break
            calls = message.get("tool_calls") or []
            messages.append({"role": "assistant", "content": message.get("content"),
                             **({"tool_calls": calls} if calls else {})})
            if not calls:
                final_text = message.get("content") or ""
                stop_reason = "model_finished"
                break
            for call in calls:
                try:
                    arguments = json.loads(call["function"]["arguments"])
                    result = tools.execute(call["function"]["name"], arguments)
                except (ValueError, KeyError, TypeError) as error:
                    result = {"error": str(error)}
                    tools.log.append({"tool": call.get("function", {}).get("name"),
                                      "error": str(error),
                                      "called_at": datetime.now(timezone.utc).isoformat()})
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        # Always reconcile from stored records, even if the model stops early.
        verification = self.manager.verify(None, order_id)
        # The model may finish before drafting. Complete this bounded business step
        # through the workflow and mark its origin so the UI does not misattribute it.
        if (stop_reason == "model_finished" and
                any(issue.code == "MISSING_SIGNATURE" for issue in verification.issues) and
                not any(entry.get("tool") == "draft_followup_message" and
                        entry.get("result", {}).get("message") for entry in tools.log)):
            tools.execute("draft_followup_message", {"order_id": order_id})
            tools.log[-1]["trigger"] = "workflow_followup"
        model_reply = final_text
        if stop_reason != "model_finished":
            final_text = "模型调用未完成；下方仅展示规则引擎的独立核验结果。"
        elif verification.issues:
            final_text = "规则核验发现：" + "；".join(
                issue.detail for issue in verification.issues) + "。请按补证任务继续处理。"
        else:
            final_text = "规则核验未发现异常，当前订单可归档。"
        agent_run_id = self.manager.save_agent_run(order_id, stop_reason, final_text, tools.log)
        return {"verification": verification.model_dump(mode="json"),
                "agent_summary": final_text, "model_reply": model_reply,
                "tool_log": tools.log,
                "agent_run_id": agent_run_id,
                "stop_reason": stop_reason,
                "stopped_at_limit": stop_reason == "step_limit"}
