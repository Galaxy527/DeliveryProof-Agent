import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pymupdf as fitz

from app.agent.client import DeepSeekClient
from app.services.document_parser import extract_document

pdf = fitz.open()
page = pdf.new_page(width=620, height=420)
page.insert_text((40, 40), "DELIVERY NOTE", fontsize=20)
page.insert_text((40, 90), "Delivery ID: DEL-VISION-001", fontsize=15)
page.insert_text((40, 120), "Order ID: ORD-001", fontsize=15)
page.insert_text((40, 150), "Customer: Zhang", fontsize=15)
page.insert_text((40, 180), "Item: CEMENT", fontsize=15)
page.insert_text((40, 210), "Quantity: 5", fontsize=15)
page.insert_text((40, 240), "Unit Price: 30 CNY", fontsize=15)
page.insert_text((40, 270), "Amount: 150 CNY", fontsize=15)
png = page.get_pixmap().tobytes("png")
result = extract_document(png, "sample-delivery.png", "delivery", DeepSeekClient())
assert result["kind"] == "delivery"
assert result["draft"]["delivery_id"] == "DEL-VISION-001"
assert result["draft"]["order_id"] == "ORD-001"
print("图片识别：送货单号与订单号提取成功")
