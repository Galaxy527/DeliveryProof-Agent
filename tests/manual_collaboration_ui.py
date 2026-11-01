"""Run the owner form with local Streamlit AppTest; no API key is needed."""
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
database = ROOT / ".tmp" / f"collab_ui_{uuid.uuid4().hex}.sqlite3"
database.parent.mkdir(exist_ok=True)
os.environ["DELIVERYPROOF_DB"] = str(database)
os.environ["DELIVERYPROOF_ADMIN_PASSWORD"] = "local-acceptance-only"

from streamlit.testing.v1 import AppTest
from app.services.collaboration import CollaborationService

app = AppTest.from_file(str(ROOT / "app/ui/streamlit_app.py"), default_timeout=30).run()
assert not app.exception and not app.error
next(x for x in app.text_input if x.label == "管理员口令").set_value("local-acceptance-only")
next(x for x in app.button if x.label == "进入工作台").click().run()
assert not app.exception and not app.error
next(x for x in app.text_input if x.label == "原始订单号 *").set_value("SO-UI-001")
next(x for x in app.text_input if x.label == "客户或工地名称 *").set_value("东区工地")
next(x for x in app.text_input if x.label == "商品 *").set_value("水泥")
next(x for x in app.number_input if x.label == "订单数量 *").set_value(100.0)
next(x for x in app.number_input if x.label == "单价（元）*").set_value(30.0)
next(x for x in app.text_input if x.label == "供应商负责人姓名 *").set_value("赵销售")
next(x for x in app.text_input if x.label.startswith("供应商负责人邮箱")).set_value("supplier@example.test")
next(x for x in app.text_input if x.label == "配送负责人姓名 *").set_value("王师傅")
next(x for x in app.text_input if x.label.startswith("配送负责人邮箱")).set_value("driver@example.test")
next(x for x in app.text_input if x.label == "收货人姓名 *").set_value("李工")
next(x for x in app.text_input if x.label.startswith("收货人邮箱")).set_value("receiver@example.test")
next(x for x in app.checkbox if "已告知上述人员" in x.label).check()
next(x for x in app.checkbox if "已对照原始订单" in x.label).check()
next(x for x in app.button if x.label == "建立订单并生成两方任务入口").click().run()
assert not app.exception and not app.error
service = CollaborationService(database)
assert service.order("SO-UI-001").customer == "东区工地"
assert len(service.participants("SO-UI-001")) == 2
driver_token = parse_qs(urlparse(service.task_link("SO-UI-001", "driver", local=True)).query)["token"][0]
receiver_token = parse_qs(urlparse(service.task_link("SO-UI-001", "receiver", local=True)).query)["token"][0]
service.close()

driver = AppTest.from_file(str(ROOT / "app/ui/streamlit_app.py"), default_timeout=30)
driver.query_params["token"] = driver_token
driver.run()
assert not driver.exception and not driver.error
assert not any(x.label == "原始订单号 *" for x in driver.text_input)
next(x for x in driver.text_input if x.label == "送货单号 *").set_value("DN-UI-001")
next(x for x in driver.number_input if x.label == "本次实送数量 *").set_value(100.0)
next(x for x in driver.button if x.label == "提交送货记录").click().run()
assert not driver.exception and not driver.error

receiver = AppTest.from_file(str(ROOT / "app/ui/streamlit_app.py"), default_timeout=30)
receiver.query_params["token"] = receiver_token
receiver.run()
assert not receiver.exception and not receiver.error
assert not any(x.label == "送货单号 *" for x in receiver.text_input)
next(x for x in receiver.checkbox if "我确认以上" in x.label).check()
next(x for x in receiver.button if x.label == "提交收货确认").click().run()
assert not receiver.exception and not receiver.error

service = CollaborationService(database)
assert service.manager.get_history("SO-UI-001")[-1]["result"]["status"] == "verified"
service.close()
next(x for x in app.button if x.label == "立即运行协调 Agent 并检查到期任务").click().run()
assert not app.exception and not app.error
assert any("检查完成" in message.value for message in app.success)
print("多方工作台：管理员建单 → 配送登记 → 客户确认 → 自动复核（通过）")
