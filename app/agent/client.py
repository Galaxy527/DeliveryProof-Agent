"""Small OpenAI-compatible DeepSeek client without another SDK dependency."""
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def _local_key() -> str | None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return None


class DeepSeekClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY") or _local_key()
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL") or
                         "https://api.deepseek.com").rstrip("/")
        if not self.api_key:
            raise ValueError("Set DEEPSEEK_API_KEY for live model calls")

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload = {"model": "deepseek-flash", "messages": messages,
                   "thinking": {"type": "disabled"}, "stream": False}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)["choices"][0]["message"]
        except urllib.error.HTTPError as error:
            # Avoid echoing server bodies that might contain request details.
            raise RuntimeError(f"DeepSeek API returned HTTP {error.code}") from error


class MockClient:
    """Offline model substitute for a reproducible tool-loop demonstration."""

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        if tools is None:
            raise ValueError("Mock mode cannot extract images")
        used = [m["tool_calls"][0]["function"]["name"] for m in messages
                if m.get("role") == "assistant" and m.get("tool_calls")]
        order_id = messages[1]["content"].split("订单 ", 1)[1].split("，", 1)[0]
        sequence = ["query_order", "match_documents", "check_evidence"]
        if len(used) < len(sequence):
            name = sequence[len(used)]
        elif used[-1] == "check_evidence":
            result = json.loads(messages[-1]["content"])
            if not result.get("issues"):
                return {"content": "规则核验未发现异常，已记录核验结果。"}
            name = "create_evidence_task"
        elif used[-1] == "create_evidence_task":
            result = json.loads(messages[-1]["content"])
            name = "draft_followup_message" if any(
                task["status"] == "open" and ":MISSING_SIGNATURE" in task["issue_id"]
                for task in result.get("tasks", [])) else None
            if name is None:
                return {"content": "已根据规则异常创建补证任务。"}
        else:
            return {"content": "已创建补证任务，并生成待人工核对的提醒草稿；没有发送消息。"}
        return {"content": None, "tool_calls": [{"id": f"mock-{len(used)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps({"order_id": order_id})}}]}

