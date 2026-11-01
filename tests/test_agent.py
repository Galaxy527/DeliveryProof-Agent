import json
from pathlib import Path

from app.agent.agent import DeliveryAgent
from app.agent.client import MockClient
from app.core.models import Batch
from app.services.task_manager import TaskManager


SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "batch.json"


def test_mock_agent_chooses_different_tool_paths(tmp_path):
    batch = Batch.model_validate_json(SAMPLE.read_text(encoding="utf-8"))
    manager = TaskManager(tmp_path / "agent.sqlite3")
    manager.import_batch(batch, trusted_demo=True)
    agent = DeliveryAgent(manager, MockClient())
    abnormal = agent.run("ORD-001")
    normal = agent.run("ORD-008")
    assert [x["tool"] for x in abnormal["tool_log"]] == [
        "query_order", "match_documents", "check_evidence", "create_evidence_task",
        "draft_followup_message"]
    draft = abnormal["tool_log"][-1]["result"]
    assert "ORD-001" in draft["message"] and "DEL-001" in draft["message"]
    assert draft["requires_human_review"] and not draft["sent"]
    assert [x["tool"] for x in normal["tool_log"]] == [
        "query_order", "match_documents", "check_evidence"]
    assert abnormal["verification"]["status"] == "needs_evidence"
    assert normal["verification"]["status"] == "verified"
    manager.close()


class FakeModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return {"content": None, "tool_calls": [{"id": "live-shaped-1", "type": "function",
                "function": {"name": "check_evidence", "arguments": json.dumps({"order_id": "ORD-001"})}}]}
        return {"content": "Model reviewed the tool result."}


def test_model_returned_tool_call_is_executed(tmp_path):
    manager = TaskManager(tmp_path / "agent.sqlite3")
    manager.import_batch(Batch.model_validate_json(SAMPLE.read_text(encoding="utf-8")), trusted_demo=True)
    result = DeliveryAgent(manager, FakeModel()).run("ORD-001")
    assert result["tool_log"][0]["tool"] == "check_evidence"
    assert len(result["tool_log"][0]["result"]["issues"]) == 2
    assert result["tool_log"][-1]["tool"] == "draft_followup_message"
    assert result["tool_log"][-1]["trigger"] == "workflow_followup"
    assert result["tool_log"][-1]["result"]["message"]
    assert result["verification"]["status"] == "needs_evidence"
    assert "规则核验发现" in result["agent_summary"]
    assert result["model_reply"] == "Model reviewed the tool result."
    manager.close()


def test_invalid_cross_order_tool_call_is_blocked(tmp_path):
    class BadModel:
        def chat(self, messages, tools):
            return {"content": None, "tool_calls": [{"id": "bad", "type": "function",
                "function": {"name": "query_order", "arguments": '{"order_id":"ORD-002"}'}}]}

    manager = TaskManager(tmp_path / "agent.sqlite3")
    manager.import_batch(Batch.model_validate_json(SAMPLE.read_text(encoding="utf-8")), trusted_demo=True)
    result = DeliveryAgent(manager, BadModel(), max_steps=2).run("ORD-001")
    assert result["stopped_at_limit"]
    assert result["tool_log"][0]["error"] == "Tool may only access the selected order"
    manager.close()
