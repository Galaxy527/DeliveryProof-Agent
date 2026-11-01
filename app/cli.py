"""Command line interface for importing, verifying and supplementing evidence."""
import argparse
import json
from pathlib import Path

from app.core.importers import load_batch
from app.core.models import Confirmation, Delivery, Order
from app.services.task_manager import TaskManager


def _emit(value, output: Path | None) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    if output:
        output.write_text(payload, encoding="utf-8")
    else:
        print(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Delivery proof verification")
    parser.add_argument("--db", type=Path, default=Path("deliveryproof.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="Import JSON/CSV data and verify all orders")
    verify.add_argument("batch", type=Path)
    verify.add_argument("--output", type=Path)
    verify.add_argument("--demo", action="store_true", help="Trust synthetic sample confirmations only")
    submit = sub.add_parser("submit", help="Persist new evidence and reverify its order")
    submit.add_argument("--delivery", type=Path)
    submit.add_argument("--confirmation", type=Path)
    submit.add_argument("--output", type=Path)
    history = sub.add_parser("history", help="Show archived verification runs")
    history.add_argument("order_id")
    tasks = sub.add_parser("tasks", help="Show evidence tasks")
    tasks.add_argument("order_id")
    agent = sub.add_parser("agent", help="Run the model-selected business tool loop")
    agent.add_argument("order_id")
    agent.add_argument("--mock", action="store_true", help="Run without API access")
    agent.add_argument("--output", type=Path)
    review = sub.add_parser("review", help="Review an uploaded customer confirmation")
    review.add_argument("source")
    review.add_argument("file", type=Path)
    review.add_argument("--actor", required=True)
    review.add_argument("--page", type=int, required=True)
    review.add_argument("--note", required=True)
    correct = sub.add_parser("correct", help="Correct a document while retaining its old version")
    correct.add_argument("kind", choices=["order", "delivery", "confirmation"])
    correct.add_argument("source")
    correct.add_argument("replacement", type=Path)
    correct.add_argument("--actor", required=True)
    correct.add_argument("--reason", required=True)
    void = sub.add_parser("void", help="Void a delivery or confirmation")
    void.add_argument("kind", choices=["delivery", "confirmation"])
    void.add_argument("source")
    void.add_argument("--actor", required=True)
    void.add_argument("--reason", required=True)
    args = parser.parse_args()
    manager = TaskManager(args.db)
    try:
        if args.command == "verify":
            if args.demo and args.batch.resolve() not in {
                (Path(__file__).resolve().parents[1] / "data/samples/batch.json").resolve(),
                (Path(__file__).resolve().parents[1] / "data/samples/csv").resolve(),
            }:
                raise ValueError("--demo 仅适用于项目内的匿名演示数据")
            batch = load_batch(args.batch)
            manager.import_batch(batch, trusted_demo=args.demo)
            result = [manager.verify(None, order.order_id).model_dump(mode="json")
                      for order in batch.orders]
            _emit(result, args.output)
        elif args.command == "submit":
            delivery = Delivery.model_validate_json(args.delivery.read_text(encoding="utf-8")) if args.delivery else None
            confirmation = Confirmation.model_validate_json(
                args.confirmation.read_text(encoding="utf-8")) if args.confirmation else None
            result = manager.submit_evidence(delivery=delivery, confirmation=confirmation)
            _emit(result.model_dump(mode="json"), args.output)
        elif args.command == "history":
            _emit(manager.get_history(args.order_id), None)
        elif args.command == "agent":
            from app.agent.agent import DeliveryAgent
            from app.agent.client import DeepSeekClient, MockClient
            client = MockClient() if args.mock else DeepSeekClient()
            result = DeliveryAgent(manager, client).run(args.order_id)
            _emit(result, args.output)
            if result["stop_reason"] != "model_finished":
                raise SystemExit(2)
        elif args.command == "review":
            _emit(manager.review_confirmation(args.source, args.file, actor=args.actor,
                                              page=args.page, note=args.note).model_dump(mode="json"), None)
        elif args.command == "correct":
            cls = {"order": Order, "delivery": Delivery, "confirmation": Confirmation}[args.kind]
            replacement = cls.model_validate_json(args.replacement.read_text(encoding="utf-8"))
            _emit(manager.correct_document(args.kind, args.source, replacement,
                                           actor=args.actor, reason=args.reason).model_dump(mode="json"), None)
        elif args.command == "void":
            _emit(manager.void_document(args.kind, args.source, actor=args.actor,
                                        reason=args.reason).model_dump(mode="json"), None)
        else:
            _emit([task.model_dump(mode="json") for task in manager.get_tasks(args.order_id)], None)
    finally:
        manager.close()


if __name__ == "__main__":
    main()
