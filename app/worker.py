"""Background follow-up loop. Start alongside the Streamlit portal."""
import argparse
import os
import time
from pathlib import Path

from app.services.collaboration import CollaborationService


def once(database: Path) -> tuple[int, int]:
    service = CollaborationService(database)
    try:
        checked = service.tick_all()
        sent = service.dispatch_queued()
        return checked, sent
    finally:
        service.close()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="持续检查交付待办并触发已配置的提醒渠道")
    parser.add_argument("--db", type=Path,
                        default=Path(os.getenv("DELIVERYPROOF_DB", str(root / "deliveryproof-collaboration.sqlite3"))))
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("检查间隔至少 30 秒")
    while True:
        checked, sent = once(args.db)
        print(f"已检查 {checked} 笔订单，已提交 {sent} 条外部通知", flush=True)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
