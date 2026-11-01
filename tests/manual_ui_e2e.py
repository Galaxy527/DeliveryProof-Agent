"""Exercise the real order → delivery → customer confirmation flow without a key."""
import os
import sys
import uuid
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
database = root / ".tmp" / f"ui_acceptance_{uuid.uuid4().hex}.sqlite3"
database.parent.mkdir(exist_ok=True)
os.environ["DELIVERYPROOF_DB"] = str(database)

import pymupdf
from streamlit.testing.v1 import AppTest
from app.services.task_manager import TaskManager


def receipt_image(delivery_id: str, quantity: int) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((60, 80), f"SYNTHETIC RECEIPT {delivery_id} {quantity} BAGS")
    return page.get_pixmap().tobytes("png")


app = AppTest.from_file(str(root / "app/ui/legacy_app.py"), default_timeout=40).run()
assert not app.exception and not app.error
assert not any("演示" in button.label for button in app.button)
next(button for button in app.button if "登记一笔已有订单" in button.label).click().run()
next(field for field in app.text_input if field.label == "订单号 *").set_value("SO-2026-001")
next(field for field in app.text_input if field.label == "客户名称").set_value("李女士")
next(field for field in app.text_input if field.label == "商品名称").set_value("水泥")
next(number for number in app.number_input if number.label == "订单数量").set_value(100.0)
next(number for number in app.number_input if number.label == "每件单价（元）").set_value(30.0)
next(button for button in app.button if "保存订单" in button.label).click().run()
assert not app.exception and not app.error

manager = TaskManager(database)
order = manager.get_batch().orders[0]
assert order.order_id == "SO-2026-001"
assert {issue["code"] for issue in manager.get_history(order.order_id)[-1]["result"]["issues"]} == {
    "MISSING_DELIVERY"}

next(button for button in app.button if "去登记送货单" in button.label).click().run()
next(field for field in app.text_input if field.label == "送货单号 *").set_value("DN-001")
next(number for number in app.number_input if number.label == "这张送货单的实送数量").set_value(95.0)
next(button for button in app.button if button.label == "登记送货单").click().run()
assert not app.exception and not app.error
assert manager.get_batch().deliveries[0].delivery_id == "DN-001"
codes = {issue["code"] for issue in manager.get_history(order.order_id)[-1]["result"]["issues"]}
assert codes == {"MISSING_SIGNATURE", "ORDER_QTY_MISMATCH:水泥"}

next(upload for upload in app.file_uploader if "上传签收文件" in upload.label).upload(
    "receipt-95.png", receipt_image("DN-001", 95), "image/png")
next(field for field in app.text_input if field.label == "您的姓名").set_value("验收员")
next(box for box in app.checkbox if "我已看过原件" in box.label).check()
next(button for button in app.button if "保存签收并重新核对" in button.label).click().run()
assert not app.exception and not app.error
assert {issue["code"] for issue in manager.get_history(order.order_id)[-1]["result"]["issues"]} == {
    "ORDER_QTY_MISMATCH:水泥"}

next(field for field in app.text_input if field.label == "送货单号 *").set_value("DN-002")
next(number for number in app.number_input if number.label == "这张送货单的实送数量").set_value(5.0)
next(button for button in app.button if button.label == "登记送货单").click().run()
assert not app.exception and not app.error
assert next(select for select in app.selectbox if "对应哪张送货单" in select.label).value == "DN-002"
next(upload for upload in app.file_uploader if "上传签收文件" in upload.label).upload(
    "receipt-5.png", receipt_image("DN-002", 5), "image/png")
next(field for field in app.text_input if field.label == "您的姓名").set_value("验收员")
next(box for box in app.checkbox if "我已看过原件" in box.label).check()
next(button for button in app.button if "保存签收并重新核对" in button.label).click().run()
assert not app.exception and not app.error
assert manager.get_history(order.order_id)[-1]["result"]["status"] == "verified"
assert all(task.status == "resolved" for task in manager.get_tasks(order.order_id))
assert len(manager.get_reviews(order.order_id)) == 2
manager.close()
print("真实流程：已有订单号 → 两张送货单号 → 分别补签收 → 任务关闭（通过）")
