"""Simple local workflow for delivery clerks; technical controls stay collapsed."""
import hashlib
import json
import os
import re
import sys
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
from app.agent.agent import DeliveryAgent
from app.agent.client import DeepSeekClient, MockClient
from app.core.importers import load_batch
from app.core.models import Batch, Confirmation, Delivery, Order
from app.services.document_parser import extract_document, finalize_draft
from app.services.task_manager import TaskManager

st.set_page_config(page_title="交付凭证工作台", page_icon="📦", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("<style>" + (ROOT / "app/ui/theme.css").read_text(encoding="utf-8") + "</style>",
            unsafe_allow_html=True)

database = Path(os.environ.get("DELIVERYPROOF_DB", str(ROOT / "deliveryproof-user.sqlite3")))
manager = TaskManager(database)

def archive_file(data: bytes, name: str) -> Path:
    path = ROOT / "uploads" / f"{hashlib.sha256(data).hexdigest()[:16]}-{Path(name).name}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path

def latest_result(order_id: str):
    runs = manager.get_history(order_id)
    return runs[-1]["result"] if runs else None

def source_label(source: str) -> str:
    if "/orders/" in f"/{source}" or source.startswith("orders/"):
        kind = "订单"
    elif "/deliveries/" in f"/{source}" or source.startswith("deliveries/"):
        kind = "送货单"
    elif "/confirmations/" in f"/{source}" or source.startswith("confirmations/"):
        kind = "客户确认"
    else:
        kind = "材料"
    return f"{kind} {Path(source).stem}"

def responsible_party(code: str) -> str:
    if code == "MISSING_SIGNATURE" or code.startswith("ACCEPTED_QTY_MISMATCH"):
        return "现场收货人确认；司机或配送人员带回材料"
    if code == "MISSING_DELIVERY" or code.startswith("ORDER_QTY_MISMATCH") or code == "DUPLICATE_DELIVERY":
        return "仓库或配送负责人核对送货记录"
    if code == "AMOUNT_MISMATCH" or code.startswith("PRICE_MISMATCH"):
        return "销售或制单人员核对金额与单价"
    return "相关业务人员核对原始记录"

def show_outcome(order_id: str):
    result = latest_result(order_id)
    if result is None:
        st.info("这笔订单还没有核对记录。")
        return
    if result["status"] == "verified":
        st.success("✅ 这笔订单已核对完成，目前没有待处理的问题。")
    else:
        st.warning(f"待处理：还有 {len(result['issues'])} 个问题需要补材料。")
        for issue in result["issues"]:
            with st.container(border=True):
                st.markdown(f"**{issue['detail']}**")
                st.write("接下来：", issue["action"])
                st.caption("建议联系：" + responsible_party(issue["code"]))
                st.caption("核对依据：" + "、".join(source_label(source) for source in issue["sources"]))

TOOL_LABELS = {
    "query_order": "查询订单要求",
    "match_documents": "查找送货单和签收记录",
    "check_evidence": "核对数量、金额和签收",
    "create_evidence_task": "建立补证待办",
    "list_open_tasks": "查看未完成待办",
    "draft_followup_message": "起草客户补证提醒",
    "reverify_delivery": "收到新材料后重新核对",
}

def show_agent_work(order_id: str):
    runs = manager.get_agent_runs(order_id)
    st.markdown("#### AI Agent 在这里做什么？")
    st.write("自动核对由固定规则完成。AI Agent 可按需要选择工具查单据、查异常、建待办，"
             "并为缺签问题起草提醒；它不会自行判断签名真伪，也不会给客户发消息。")
    if st.button("让 AI Agent 处理这笔订单", key=f"run_main_agent_{order_id}"):
        try:
            with st.spinner("Agent 正在选择工具并处理这笔订单…"):
                agent_result = DeliveryAgent(manager, DeepSeekClient()).run(order_id)
            if agent_result["stop_reason"] != "model_finished":
                st.error("AI 服务未完成处理；订单的规则核对仍可正常使用。")
            else:
                st.success("Agent 已完成处理。下方可以看到它实际调用的工具。")
            runs = manager.get_agent_runs(order_id)
        except Exception as error:
            st.error("AI Agent 暂时不可用：" + str(error))
    if not runs:
        st.caption("这笔订单还没有运行过 AI Agent。上面的规则核对和补证流程仍会正常工作。")
        return
    run = runs[-1]
    if run["stop_reason"] != "model_finished":
        st.warning("最近一次 Agent 未完整运行；以下记录只代表已执行的步骤。")
    for index, entry in enumerate(run["tool_log"], start=1):
        label = TOOL_LABELS.get(entry.get("tool"), "未识别的工具")
        if entry.get("trigger") == "workflow_followup":
            label += "（工作流补充执行）"
        st.write(f"{index}. {label}" + ("（失败）" if entry.get("error") else "（已执行）"))
    drafts = [entry["result"]["message"] for entry in run["tool_log"]
              if entry.get("tool") == "draft_followup_message" and
              entry.get("result", {}).get("message")]
    if drafts:
        st.text_area("待人工核对的客户补证提醒", drafts[-1], height=125,
                     key=f"agent_draft_{order_id}_{run['run_id']}")
        st.caption("这只是草稿，系统没有发送给客户。")
    with st.expander("查看 Agent 的详细工具记录"):
        st.json(run["tool_log"])

if st.session_state.get("ui_version") != 4:
    st.session_state["ui_version"] = 4
    st.session_state["screen"] = "home"

nav_left, nav_right = st.columns([4, 1])
with nav_left:
    st.markdown('<div class="app-nav"><div class="app-logo">D</div><div class="app-brand">交付凭证工作台<small>DELIVERY PROOF / LOCAL WORKSPACE</small></div></div>', unsafe_allow_html=True)
with nav_right:
    if st.session_state["screen"] == "home":
        st.markdown('<span class="app-nav-right">本地工作台</span>', unsafe_allow_html=True)
    elif st.button("← 返回开始页", width="stretch"):
        st.session_state["screen"] = "home"
        st.rerun()
st.markdown('<div class="top-rule"></div>', unsafe_allow_html=True)

if st.session_state["screen"] == "home":
    st.markdown('<section class="hero"><div class="eyebrow">交付后 · 对账前</div><h1>每一笔交付，<br><em>都有据可查。</em></h1><p>把已有订单、送货单和客户签收放在一起核对。缺少材料或数量不符时，留下待办，收到补充资料后继续复核。</p></section>', unsafe_allow_html=True)
    st.markdown('<div class="section-head"><div class="kicker">WORKSPACE</div><h2>开始核对交付资料</h2><p>先登记已有订单，后续按实际收到的业务单据逐项补充。</p></div>', unsafe_allow_html=True)
    if st.button("登记一笔已有订单 →", type="primary", width="stretch"):
        st.session_state["screen"] = "new_order"
        st.rerun()
    if manager.get_batch().orders and st.button("继续处理已有订单 →"):
        st.session_state["screen"] = "workspace"
        st.rerun()
    st.markdown('<div class="flow-strip"><span><b>01</b> 销售/业务系统提供订单</span><span><b>02</b> 仓库/配送提供送货单</span><span><b>03</b> 客户确认收货</span><span><b>04</b> 内勤核对并跟进</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="page-note"><strong>需要准备什么？</strong> 订单号、客户、商品、数量和单价来自现有订单；送货单号与实送数量来自仓库或配送记录。缺签时，每张送货单再补 1 份已签收的送货单或其他客户收货确认图片/PDF，不一定另有“收货单”。不同角色可在不同时间提供，内勤负责核对与跟进。</div>', unsafe_allow_html=True)
    st.stop()

if st.session_state["screen"] == "new_order":
    st.markdown('<section class="hero compact"><div class="eyebrow">第一步 / 订单资料</div><h1>登记已有订单</h1><p>按销售记录或业务系统上的订单填写，保存后再等送货记录。</p></section>', unsafe_allow_html=True)
    st.caption("请照现有订单转录。保存后，仓库或配送人员提供送货单，再登记实送数量和客户签收。")
    with st.form("new_business_form"):
        order_number = st.text_input("订单号 *", placeholder="例如：ORD-2026-001")
        customer_name = st.text_input("客户名称", placeholder="例如：张先生")
        product_name = st.text_input("商品名称", placeholder="例如：水泥")
        x, z = st.columns(2)
        ordered_qty = x.number_input("订单数量", min_value=1.0, value=None, step=1.0,
                                     placeholder="按原始订单填写")
        unit_price = z.number_input("每件单价（元）", min_value=0.0, value=None, step=1.0,
                                    placeholder="按原始订单填写")
        create_business = st.form_submit_button("保存订单，等待送货记录", type="primary")
    if create_business:
        try:
            order_id = order_number.strip()
            if not order_id or not customer_name.strip() or not product_name.strip():
                raise ValueError("请填写订单号、客户名称和商品名称。")
            if ordered_qty is None or unit_price is None:
                raise ValueError("请按原始订单填写数量和单价，不要使用猜测数值。")
            if not re.fullmatch(r"[\w-]{1,40}", order_id):
                raise ValueError("订单号只能使用文字、数字、下划线或短横线，最多 40 个字符。")
            if any(existing.order_id == order_id for existing in manager.get_batch().orders):
                raise ValueError("这个订单号已经存在，请填写现有业务单据上的另一个订单号。")
            order = Order(order_id=order_id, customer=customer_name.strip(), currency="CNY",
                          items=[{"sku": product_name.strip(), "quantity": str(ordered_qty),
                                  "unit_price": str(unit_price)}],
                          source=f"manual/orders/{order_id}.json")
            manager.import_batch(Batch(orders=[order], deliveries=[], confirmations=[]))
            manager.verify(None, order_id)
            st.session_state["selected_order"] = order_id
            st.session_state["view"] = "① 看问题"
            st.session_state["screen"] = "workspace"
            st.session_state["notice"] = "订单号已登记。下一步请登记仓库或配送提供的送货单。"
            st.rerun()
        except Exception as error:
            st.error(str(error))
    st.stop()
st.markdown('<section class="hero compact"><div class="eyebrow">业务工作台 / 当前订单</div><h1>核对交付资料</h1><p>看差异、追材料、复核结果。各方提供原始记录，内勤在这里跟进处理。</p></section>', unsafe_allow_html=True)
batch = manager.get_batch()
orders = batch.orders
if "notice" in st.session_state:
    st.success(st.session_state["notice"])
    del st.session_state["notice"]

if not orders:
    st.info("还没有登记订单。请返回开始页，登记第一笔已有订单。")
    if st.button("去登记订单", type="primary"):
        st.session_state["screen"] = "new_order"
        st.rerun()
else:
    order_ids = [o.order_id for o in orders]
    results_by_order = {oid: latest_result(oid) for oid in order_ids}
    pending = [oid for oid in order_ids if results_by_order[oid] and
               results_by_order[oid]["status"] != "verified"]
    default = order_ids.index(pending[0]) if pending else 0
    if "next_view" in st.session_state:
        st.session_state["view"] = st.session_state.pop("next_view")
    if "next_delivery" in st.session_state:
        st.session_state["delivery_for_signature"] = st.session_state.pop("next_delivery")
    select_options = {"key": "selected_order"}
    if "selected_order" not in st.session_state:
        select_options["index"] = default
    selected_order = st.selectbox("选择要处理的订单", order_ids,
                                  format_func=lambda oid: f"{oid} · {next(o.customer for o in orders if o.order_id == oid)}",
                                  **select_options)
    order = next(o for o in orders if o.order_id == selected_order)
    current = results_by_order[selected_order]
    issue_count = len(current["issues"]) if current else 0
    a, b, c = st.columns(3)
    a.metric("客户", order.customer)
    b.metric("商品", "、".join(item.sku for item in order.items))
    c.metric("待解决问题", issue_count)

    st.radio("办理步骤", ["① 看问题", "② 补材料", "③ 看结果"],
             horizontal=True, key="view", label_visibility="collapsed")

    if st.session_state["view"] == "① 看问题":
        st.subheader("这笔订单现在怎么样？")
        related_deliveries = [d for d in batch.deliveries if d.order_id == selected_order]
        ordered = "、".join(f"{item.quantity} {item.sku}" for item in order.items)
        delivered = "、".join(f"{item.quantity} {item.sku}" for delivery in related_deliveries
                             for item in delivery.items) or "还没有送货记录"
        st.caption(f"订单要求：{ordered}　｜　送货记录：{delivered}　｜　下方列出还需要处理的问题")
        show_outcome(selected_order)
        if issue_count:
            next_action = ("去登记送货单 →" if any(issue["code"] == "MISSING_DELIVERY"
                                             for issue in current["issues"]) else "下一步：处理待办 →")
            if st.button(next_action, type="primary"):
                st.session_state["next_view"] = "② 补材料"
                st.rerun()
        else:
            if st.button("查看完整记录", type="primary"):
                st.session_state["next_view"] = "③ 看结果"
                st.rerun()
        st.divider()
        show_agent_work(selected_order)

    elif st.session_state["view"] == "② 补材料":
        st.subheader("把缺少的材料补上")
        st.markdown('<div class="hint">处理完一项，系统会立即重新核对订单。</div>', unsafe_allow_html=True)
        deliveries = [d for d in manager.get_batch().deliveries if d.order_id == selected_order]
        codes = {issue["code"] for issue in current["issues"]} if current else set()

        if "MISSING_SIGNATURE" in codes:
            st.markdown("#### 上传客户签收")
            st.write("现场收货人确认后，由司机、配送人员或客户把材料交给内勤。可以是已签字的送货单，也可以是另附的收货确认；每张送货单在这里登记 1 份对应图片/PDF。")
            with st.form("signature_form"):
                delivery_id = st.selectbox("对应哪张送货单", [d.delivery_id for d in deliveries],
                                           key="delivery_for_signature")
                delivery = next(d for d in deliveries if d.delivery_id == delivery_id)
                item = delivery.items[0]
                accepted = st.number_input(f"客户确认收到多少 {item.sku}", min_value=0.0,
                                           value=float(item.quantity), step=1.0)
                proof = st.file_uploader("上传签收文件（图片或 PDF）", type=["pdf", "png", "jpg", "jpeg", "webp"])
                actor = st.text_input("您的姓名")
                page = st.number_input("签收在第几页", min_value=1, step=1)
                checked = st.checkbox("我已看过原件，确认有客户签名或明确的收货确认")
                submitted = st.form_submit_button("保存签收并重新核对", type="primary")
            if submitted:
                try:
                    if not proof or not actor.strip() or not checked or accepted <= 0:
                        raise ValueError("请上传文件、填写姓名和数量，并确认已核对原件。")
                    file_path = archive_file(proof.getvalue(), proof.name)
                    ref = file_path.relative_to(ROOT).as_posix()
                    number = uuid.uuid4().hex[:8]
                    confirmation = Confirmation(
                        confirmation_id=f"CONF-{number}", delivery_id=delivery_id,
                        items=[{"sku": item.sku, "quantity": str(accepted)}],
                        evidence_ref=ref, source=f"manual/confirmations/CONF-{number}.json")
                    manager.submit_evidence(confirmation=confirmation)
                    result = manager.review_confirmation(confirmation.source, file_path,
                                                         actor=actor, page=int(page),
                                                         note="已对照上传文件确认客户签收与数量")
                    st.session_state["notice"] = ("签收材料已保存，这笔订单已核对完成。" if result.status == "verified"
                                                  else "签收材料已保存，订单已重新核对；请继续处理剩余问题。")
                    st.session_state["next_view"] = "③ 看结果" if result.status == "verified" else "② 补材料"
                    st.rerun()
                except Exception as error:
                    st.error(str(error))

        if "MISSING_DELIVERY" in codes or any(code.startswith("ORDER_QTY_MISMATCH") for code in codes):
            st.markdown("#### 登记已有送货单" if not deliveries else "#### 登记补送记录")
            st.write("送货单号和实送数量应来自仓库或配送记录。尚未拿到送货单时，可先保留这项待办，不要猜填。")
            existing_qty = sum((item.quantity for delivery in deliveries for item in delivery.items
                                if item.sku == order.items[0].sku), Decimal("0"))
            remaining_qty = order.items[0].quantity - existing_qty
            if remaining_qty < 0:
                st.warning("当前送货数量已超过订单。请先向仓库或销售核对原始单据，再使用下方高级操作更正。")
            else:
                with st.form("delivery_form"):
                    delivery_number = st.text_input("送货单号 *", placeholder="例如：DEL-2026-001")
                    line = st.selectbox("送货商品", order.items, format_func=lambda item: item.sku)
                    qty = st.number_input("这张送货单的实送数量", min_value=0.0,
                                          value=None, step=1.0, placeholder="按送货单填写")
                    submit_delivery = st.form_submit_button("登记送货单", type="primary")
                if submit_delivery:
                    try:
                        delivery_id = delivery_number.strip()
                        if not re.fullmatch(r"[\w-]{1,40}", delivery_id):
                            raise ValueError("请填写原始送货单号，只能使用文字、数字、下划线或短横线，最多 40 个字符。")
                        if qty is None or qty <= 0:
                            raise ValueError("实送数量必须大于 0。")
                        if any(d.delivery_id == delivery_id for d in manager.get_batch().deliveries):
                            raise ValueError("这个送货单号已经存在，请核对原始单据。")
                        delivery = Delivery(delivery_id=delivery_id, order_id=selected_order,
                                            customer=order.customer,
                                            items=[{"sku": line.sku, "quantity": str(qty),
                                                    "unit_price": line.unit_price}],
                                            declared_amount=Decimal(str(qty)) * line.unit_price,
                                            source=f"manual/deliveries/{delivery_id}.json")
                        manager.submit_evidence(delivery=delivery)
                        st.session_state["next_delivery"] = delivery.delivery_id
                        st.session_state["notice"] = "送货单号已登记。收到对应客户签收后，再上传确认材料。"
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))

        with st.expander("从送货单图片或 PDF 自动读取信息（可选）"):
            kind = st.selectbox("文件是什么", ["delivery", "confirmation"],
                                format_func=lambda x: "送货单" if x == "delivery" else "客户签收单")
            uploaded = st.file_uploader("选择文件", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                        key="auto_extract")
            if uploaded and st.button("读取文件内容"):
                try:
                    st.session_state["extracted"] = extract_document(
                        uploaded.getvalue(), uploaded.name, kind, DeepSeekClient())
                    st.session_state["extracted_bytes"] = uploaded.getvalue()
                except Exception as error:
                    st.error(str(error))
            if "extracted" in st.session_state:
                extracted = st.session_state["extracted"]
                st.warning("请对照原件确认，系统不会直接相信识别结果。")
                st.write("识别疑点：", extracted.get("review_notes", []))
                draft = extracted["draft"]
                doc_id = st.text_input("单据编号", value=draft.get("delivery_id") or draft.get("confirmation_id") or "")
                related = st.text_input("关联订单号" if kind == "delivery" else "关联送货单号",
                                        value=draft.get("order_id") or draft.get("delivery_id") or "")
                item = (draft.get("items") or [{}])[0]
                sku = st.text_input("商品", value=item.get("sku") or "")
                amount = st.text_input("数量", value=str(item.get("quantity") or ""))
                price = st.text_input("单价", value=str(item.get("unit_price") or "")) if kind == "delivery" else ""
                signed = st.checkbox("我确认文件里有客户签名或明确收货确认", key="auto_signed") if kind == "confirmation" else False
                reviewer = st.text_input("审核人", key="auto_reviewer") if signed else ""
                page_number = st.number_input("签收页码", min_value=1, key="auto_page") if signed else 1
                if st.button("确认内容并保存", type="primary"):
                    try:
                        file_path = ROOT / extracted["source"]
                        file_path.parent.mkdir(exist_ok=True)
                        file_path.write_bytes(st.session_state["extracted_bytes"])
                        if kind == "delivery":
                            parsed = Delivery(delivery_id=doc_id, order_id=related,
                                customer=draft.get("customer") or order.customer,
                                items=[{"sku": sku, "quantity": amount, "unit_price": price}],
                                declared_amount=Decimal(amount) * Decimal(price), source=extracted["source"])
                            manager.submit_evidence(delivery=parsed)
                        else:
                            parsed = Confirmation(confirmation_id=doc_id, delivery_id=related,
                                items=[{"sku": sku, "quantity": amount}],
                                evidence_ref=extracted["source"] if signed else None,
                                source=extracted["source"])
                            manager.submit_evidence(confirmation=parsed)
                            if signed:
                                manager.review_confirmation(parsed.source, file_path, actor=reviewer,
                                    page=int(page_number), note="已对照上传文件确认客户签收")
                        st.session_state["notice"] = "文件已保存，订单已重新核对。"
                        del st.session_state["extracted"]
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))
        if st.button("查看最新结果"):
            st.session_state["next_view"] = "③ 看结果"
            st.rerun()

    else:
        st.subheader("核对结果和处理记录")
        show_outcome(selected_order)
        tasks = manager.get_tasks(selected_order)
        if tasks:
            st.markdown("#### 处理过的问题")
            for task in tasks:
                st.write(("✅ 已解决" if task.status == "resolved" else "⏳ 仍待处理")
                         + " · " + task.action)
        with st.expander("查看每次核对的时间和记录"):
            for run in reversed(manager.get_history(selected_order)):
                st.write(f"第 {run['run_id']} 次 · {run['created_at']} · "
                         + ("已完成" if run["result"]["status"] == "verified" else "待补材料"))
        with st.expander("查看签收审核详情"):
            st.dataframe(manager.get_reviews(selected_order), hide_index=True, width="stretch")

with st.expander("高级操作：批量导入、Agent 和单据更正"):
    st.caption("只有需要处理大量数据或检查详细记录时才使用这里。")
    advanced_import, advanced_agent, advanced_edit = st.tabs(["批量导入", "Agent 运行记录", "更正单据"])
    with advanced_import:
        json_file = st.file_uploader("上传 JSON 数据", type=["json"], key="json_batch")
        if json_file and st.button("导入 JSON"):
            try:
                incoming = Batch.model_validate_json(json_file.getvalue())
                manager.import_batch(incoming)
                for item in incoming.orders:
                    manager.verify(None, item.order_id)
                st.success("数据已导入；未经人工审核的签收引用仍会提示补证。")
            except Exception as error:
                st.error(str(error))
        csv_files = st.file_uploader("上传 orders.csv、deliveries.csv、confirmations.csv",
                                     type=["csv"], accept_multiple_files=True)
        if csv_files and st.button("导入 CSV"):
            try:
                if {file.name for file in csv_files} != {"orders.csv", "deliveries.csv", "confirmations.csv"}:
                    raise ValueError("请上传全部三个指定名称的 CSV 文件。")
                folder = ROOT / ".tmp" / "ui_csv"
                folder.mkdir(parents=True, exist_ok=True)
                for file in csv_files:
                    (folder / file.name).write_bytes(file.getvalue())
                incoming = load_batch(folder)
                manager.import_batch(incoming)
                for item in incoming.orders:
                    manager.verify(None, item.order_id)
                st.success("CSV 已导入并完成初次核对。")
            except Exception as error:
                st.error(str(error))
    with advanced_agent:
        if orders:
            agent_order = st.selectbox("要分析的订单", [item.order_id for item in orders])
            mode = st.radio("运行方式", ["离线模拟", "DeepSeek Flash"], horizontal=True)
            if st.button("运行 Agent"):
                client = MockClient() if mode == "离线模拟" else DeepSeekClient()
                result = DeliveryAgent(manager, client).run(agent_order)
                if result["stop_reason"] != "model_finished":
                    st.error("模型未正常完成；下面仅是规则核对的结果：" + result["stop_reason"])
                st.write(result["agent_summary"])
                with st.expander("工具调用详情"):
                    st.json(result["tool_log"])
                if result.get("model_reply"):
                    with st.expander("模型原文（仅供核查）"):
                        st.write(result["model_reply"])
            for run in reversed(manager.get_agent_runs(agent_order)[-5:]):
                with st.expander(f"运行 {run['run_id']} · {run['stop_reason']}"):
                    st.write(run["summary"])
                    st.json(run["tool_log"])
    with advanced_edit:
        if orders:
            edit_order = st.selectbox("选择订单", [item.order_id for item in orders], key="edit_order")
            documents = manager.get_documents(edit_order)
            if documents:
                chosen = st.selectbox("选择单据", documents,
                    format_func=lambda d: f"{d['document_id']} · {'有效' if d['active'] else '已作废'}")
                st.caption("更正会保留旧版内容；作废不会删除历史。")
                actor = st.text_input("操作人", key="edit_actor")
                reason = st.text_input("更正或作废原因", key="edit_reason")
                edited = st.text_area("更正后的完整单据", json.dumps(chosen["payload"], ensure_ascii=False, indent=2),
                                      height=220, key=f"edit_{chosen['source']}")
                if st.button("保存更正", disabled=not chosen["active"]):
                    try:
                        cls = {"order": Order, "delivery": Delivery,
                               "confirmation": Confirmation}[chosen["kind"]]
                        obj = cls.model_validate(json.loads(edited))
                        manager.correct_document(chosen["kind"], chosen["source"], obj,
                                                 actor=actor, reason=reason)
                        st.success("单据已更正，并已重新核对。")
                    except Exception as error:
                        st.error(str(error))
                if st.button("作废这张单据", disabled=not chosen["active"] or chosen["kind"] == "order"):
                    try:
                        manager.void_document(chosen["kind"], chosen["source"], actor=actor, reason=reason)
                        st.success("单据已作废，并已重新核对。")
                    except Exception as error:
                        st.error(str(error))
                with st.expander("更正与作废历史"):
                    st.json(manager.get_document_events(chosen["source"]))
