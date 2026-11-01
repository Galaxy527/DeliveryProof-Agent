"""Three-party portal for a single supplier's delivery cases."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st
from app.agent.client import DeepSeekClient
from app.agent.investigation import InvestigationAgent
from app.core.models import Order
from app.services.collaboration import CollaborationService, ROLES
from app.services.document_parser import extract_document

st.set_page_config(page_title="交付协作工作台", page_icon="📦", layout="wide")
st.markdown("<style>" + (ROOT / "app/ui/theme.css").read_text(encoding="utf-8") + "</style>",
            unsafe_allow_html=True)

database = Path(os.getenv("DELIVERYPROOF_DB", str(ROOT / "deliveryproof-collaboration.sqlite3")))
service = CollaborationService(database)
password_file = ROOT / ".tmp" / "owner-password.txt"
password_file.parent.mkdir(parents=True, exist_ok=True)
if not os.getenv("DELIVERYPROOF_ADMIN_PASSWORD") and not password_file.exists():
    password_file.write_text(secrets.token_urlsafe(24), encoding="utf-8")
owner_password = os.getenv("DELIVERYPROOF_ADMIN_PASSWORD") or password_file.read_text(encoding="utf-8")


def summary(order_id: str) -> None:
    order = service.order(order_id)
    history = service.manager.get_history(order_id)
    result = history[-1]["result"] if history else service.run_agent(order_id)
    resolved = {r["issue_id"]: r for r in service.resolutions(order_id)}
    open_issues = [i for i in result["issues"] if i["issue_id"] not in resolved]
    delivered = service.deliveries(order_id)
    sent_qty = sum((i.quantity for d in delivered for i in d.items), Decimal(0))
    a, b, c = st.columns(3)
    a.metric("订单", order_id)
    b.metric("已登记送货", f"{sent_qty} / {order.items[0].quantity} {order.items[0].sku}")
    c.metric("待处理问题", len(open_issues))
    if order.delivery_due_date:
        st.caption(f"约定交付日期：{order.delivery_due_date} · "
                   + ("允许分批送货" if order.allow_split_delivery else "一次交付"))
    if not result["issues"]:
        st.success("当前订单资料已核对完成。")
    elif not open_issues:
        st.info("业务差异均已记录处理依据；原始差异保留在核验记录中。")
    else:
        for issue in open_issues:
            with st.container(border=True):
                st.markdown(f"**{issue['detail']}**")
                st.caption(f"处理建议：{issue['action']}")
                st.caption("依据：" + "、".join(issue["sources"]))
    if resolved:
        with st.expander(f"查看已处理差异（{len(resolved)}）"):
            for issue in result["issues"]:
                if issue["issue_id"] in resolved:
                    st.write(f"{issue['detail']} · 处理记录：{resolved[issue['issue_id']]['explanation']}")


def owner_page() -> None:
    st.markdown('<section class="hero compact"><div class="eyebrow">供应商 / 订单负责人</div>'
                '<h1>交付协作工作台</h1><p>建立订单，邀请配送与收货人员，跟进每次交付。</p></section>',
                unsafe_allow_html=True)
    if st.button("退出供应商工作台"):
        st.session_state["owner_authenticated"] = False
        st.rerun()
    create_tab, cases_tab = st.tabs(["建立订单与分配任务", "查看交付进度"])
    with create_tab:
        st.subheader("1. 登记已有订单")
        st.caption("可上传订单图片/PDF 辅助提取；识别结果须人工核对。也可以直接按原始订单填写。")
        upload = st.file_uploader("原始订单文件（可选）", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                  key="owner_order_upload")
        if upload and st.button("从订单文件提取字段"):
            try:
                extracted = extract_document(upload.getvalue(), upload.name, "order", DeepSeekClient())
                st.session_state["order_draft"] = extracted["draft"]
                st.session_state["order_notes"] = extracted["review_notes"]
                st.success("已生成待核对草稿。请对照原件检查下面每一项。")
            except Exception as error:
                st.error(f"无法提取订单：{error}。仍可手动填写。")
        if st.session_state.get("order_notes"):
            st.info("识别疑点：" + "；".join(map(str, st.session_state["order_notes"])))
        draft = st.session_state.get("order_draft") or {}
        item = (draft.get("items") or [{}])[0]
        with st.form("owner_create_case"):
            order_id = st.text_input("原始订单号 *", value=draft.get("order_id") or "",
                                     placeholder="例如 SO-2026-001")
            customer = st.text_input("客户或工地名称 *", value=draft.get("customer") or "")
            x, y, z = st.columns(3)
            sku = x.text_input("商品 *", value=item.get("sku") or "")
            quantity = y.number_input("订单数量 *", min_value=0.01,
                                      value=float(item["quantity"]) if item.get("quantity") else None)
            price = z.number_input("单价（元）*", min_value=0.0,
                                   value=float(item["unit_price"]) if item.get("unit_price") else None)
            due_date = st.date_input("约定最晚交付日期 *", value=date.today() + timedelta(days=3))
            split = st.checkbox("合同允许分批送货", value=True)
            st.markdown("#### 供应商订单负责人")
            st.caption("以下每位联系人都须填写邮箱或电话，至少一项。")
            owner_name = st.text_input("供应商负责人姓名 *")
            owner_email = st.text_input("供应商负责人邮箱（与电话至少填一项）")
            owner_phone = st.text_input("供应商负责人电话（与邮箱至少填一项）")
            st.markdown("#### 配送联系人")
            driver_name = st.text_input("配送负责人姓名 *")
            driver_email = st.text_input("配送负责人邮箱（与电话至少填一项）", placeholder="用于发送任务通知")
            driver_phone = st.text_input("配送负责人电话（与邮箱至少填一项）", placeholder="可作为超时升级联系渠道")
            st.markdown("#### 客户指定收货人")
            receiver_name = st.text_input("收货人姓名 *")
            receiver_email = st.text_input("收货人邮箱（与电话至少填一项）")
            receiver_phone = st.text_input("收货人电话（与邮箱至少填一项）")
            contact_allowed = st.checkbox("已告知上述人员：联系方式用于本订单的交付任务与提醒 *")
            reviewed = st.checkbox("已对照原始订单核对所有字段 *")
            create = st.form_submit_button("建立订单并生成两方任务入口", type="primary")
        if create:
            try:
                if not reviewed or not contact_allowed:
                    raise ValueError("请确认已核对订单，并已告知联系人通知用途")
                if not re.fullmatch(r"[\w-]{1,40}", order_id.strip()):
                    raise ValueError("原始订单号只能含文字、数字、下划线和短横线，最多 40 字符")
                if not customer.strip() or not sku.strip() or quantity is None or price is None:
                    raise ValueError("请填写客户、商品、订单数量与单价")
                if due_date < date.today():
                    raise ValueError("交付日期不能早于今天")
                order = Order(order_id=order_id.strip(), customer=customer.strip(),
                              items=[{"sku": sku.strip(), "quantity": str(quantity),
                                      "unit_price": str(price)}],
                              source=f"manual/orders/{order_id.strip()}.json",
                              delivery_due_date=due_date, allow_split_delivery=split)
                contacts = {
                    "supplier": {"name": owner_name, "email": owner_email, "phone": owner_phone,
                                 "contact_allowed": contact_allowed},
                    "driver": {"name": driver_name, "email": driver_email, "phone": driver_phone,
                               "contact_allowed": contact_allowed},
                    "receiver": {"name": receiver_name, "email": receiver_email, "phone": receiver_phone,
                                 "contact_allowed": contact_allowed},
                }
                service.create_order(order, contacts,
                    source_file=(upload.name, upload.getvalue()) if upload else None)
                service.run_agent(order.order_id)
                service.dispatch_queued()
                st.session_state["new_case_id"] = order.order_id
                st.session_state.pop("order_draft", None)
                st.success("订单已建立。请将下面的专属入口发给对应人员。")
            except Exception as error:
                st.error(str(error))
        if st.session_state.get("new_case_id"):
            selected = st.session_state["new_case_id"]
            st.markdown(f"#### 订单 {selected} 的专属入口")
            st.caption("链接是访问凭证。只发给对应人员；转发或泄露后请重新生成。")
            for role, label in ROLES.items():
                link = service.task_link(selected, role) or service.task_link(selected, role, local=True)
                st.text_input(f"{label}入口", value=link or "", key=f"invite_{selected}_{role}")
            st.caption("本地地址仅供本机测试；要让外部人员访问，需部署 HTTPS 并配置 DELIVERYPROOF_PUBLIC_URL。")
    with cases_tab:
        orders = service.orders()
        if not orders:
            st.info("还没有订单。请先在左侧页签登记。")
            return
        selected = st.selectbox("选择订单", [o.order_id for o in orders], key="owner_order_select")
        summary(selected)
        if st.button("检查订单与到期任务", type="primary"):
            with st.spinner("正在检查订单与到期任务…"):
                result = service.run_agent(selected)
                sent = service.dispatch_queued()
            pending = len([n for n in service.notifications(selected)
                           if n["status"] in {"queued", "manual"}])
            st.session_state["agent_notice"] = (
                f"检查完成：当前有 {len(result['issues'])} 项待处理问题，"
                f"本次交给发送服务 {sent} 条通知，另有 {pending} 条待发送或需人工转发。")
            st.rerun()
        if st.session_state.get("agent_notice"):
            st.success(st.session_state.pop("agent_notice"))
        st.markdown("#### AI 异常调查")
        st.caption("用于数量或收货争议：AI 先查看当前记录和各方说明，再提出一项补证建议。"
                   "每次点击才调用模型；建议不会自动发送或结案。")
        if st.button("调查当前差异并提出下一步", key=f"investigate_{selected}"):
            try:
                with st.spinner("正在查询证据并生成待审核建议…"):
                    outcome = InvestigationAgent(service, DeepSeekClient()).run(selected)
                if outcome["status"] == "draft":
                    st.success("已形成调查草稿。请核对下方依据和请求内容，再决定是否批准。")
                else:
                    st.info("本次没有形成有效补证建议；原因：" + outcome["stop_reason"])
                st.caption(f"本次模型调用 {outcome['model_calls']} 次；"
                           f"接口报告消耗 {outcome['total_tokens']} tokens。")
            except Exception as error:
                st.error(f"调查未完成：{type(error).__name__}。请确认模型密钥和网络连接。")
        investigations = service.investigations(selected)
        if investigations:
            status_labels = {"draft": "待供应商审批", "approved": "已批准，等待回复",
                             "answered": "已回复，可再次调查", "stale": "资料已变化，请重新调查"}
            for draft in investigations[:8]:
                with st.container(border=True):
                    st.write(f"调查 #{draft['id']} · {status_labels.get(draft['status'], draft['status'])}"
                             f" · 对象：{ROLES.get(draft['target_role'], '供应商负责人')}")
                    st.write(draft["request"])
                    st.caption("调查依据：" + draft["reason"])
                    st.caption("引用：" + "、".join(draft["evidence_refs"]))
                    if draft["status"] == "draft":
                        if st.button("审核通过并发出补证请求", key=f"approve_{draft['id']}"):
                            try:
                                service.approve_investigation(selected, draft["id"])
                                service.dispatch_queued()
                                st.success("请求已批准。发送状态请查看下方提醒记录。")
                                st.rerun()
                            except Exception as error:
                                st.error(str(error))
                    if draft["status"] == "approved" and draft["target_role"] == "supplier":
                        with st.form(f"supplier_reply_{draft['id']}"):
                            reply = st.text_area("供应商核查结果 *", key=f"supplier_note_{draft['id']}")
                            attachment = st.file_uploader("核查依据照片或 PDF（可选）",
                                type=["pdf", "png", "jpg", "jpeg", "webp"],
                                key=f"supplier_file_{draft['id']}")
                            if st.form_submit_button("提交核查结果"):
                                try:
                                    service.supplier_respond_to_investigation(selected, draft["id"], reply,
                                        evidence=(attachment.name, attachment.getvalue()) if attachment else None)
                                    st.success("核查结果已记录。可以再次运行 AI 调查。")
                                    st.rerun()
                                except Exception as error:
                                    st.error(str(error))
            with st.expander("查看调查回复"):
                responses = service.investigation_responses(selected)
                if not responses:
                    st.caption("还没有补证回复。")
                for response in responses:
                    st.write(f"{ROLES.get(response['role'], '供应商负责人')}：{response['note']}")
                    st.caption(f"来源 {response['ref']} · {response['submitted_at']}")
            with st.expander("查看 AI 查询过程和调用量"):
                tool_labels = {"get_case_snapshot": "查询订单与差异",
                               "list_evidence": "查看证据目录",
                               "get_party_statements": "查看各方说明",
                               "propose_next_step": "提出下一步补证建议"}
                for run in service.investigation_runs(selected)[:5]:
                    steps = " → ".join(tool_labels.get(e["tool"], e["tool"])
                                       for e in run["tool_log"])
                    st.write(f"{run['created_at']} · {steps or run['status']}")
                    st.caption(f"模型调用 {run['model_calls']} 次，接口报告消耗 "
                               f"{run['total_tokens']} tokens；状态：{run['status']}")
        with st.expander("查看并下载原始附件"):
            files = service.evidence_files(selected)
            if not files:
                st.caption("这笔订单还没有上传附件。")
            for index, file in enumerate(files):
                st.download_button(file["label"], data=file["path"].read_bytes(),
                                   file_name=file["filename"], key=f"evidence_{selected}_{index}")
        st.markdown("#### 凭证审核与差异处理")
        st.caption("在线收货反馈只表示有人通过专属链接填写；签收凭证由供应商另行核对。")
        reviews = {c.source for c in service.manager.get_batch(reviewed_only=True).confirmations
                   if c.evidence_ref}
        for doc in service.manager.get_documents(selected):
            if doc["kind"] != "confirmation" or not doc["active"] or doc["source"] in reviews:
                continue
            delivery_id = doc["payload"]["delivery_id"]
            with st.expander(f"审核 {delivery_id} 的收货凭证"):
                with st.form(f"review_{doc['source']}"):
                    proof = st.file_uploader("签收照片或 PDF *", type=["pdf", "png", "jpg", "jpeg", "webp"])
                    note = st.text_area("审核依据 *", placeholder="例如：已对照原始签收单及收货数量")
                    if st.form_submit_button("确认审核凭证"):
                        try:
                            if not proof:
                                raise ValueError("请先上传签收凭证")
                            service.review_receipt(selected, delivery_id, proof.name, proof.getvalue(), note=note)
                            st.success("凭证已审核，系统已重新核对。")
                            st.rerun()
                        except Exception as error:
                            st.error(str(error))
        latest = service.manager.get_history(selected)[-1]["result"]
        closed = {r["issue_id"] for r in service.resolutions(selected)}
        for issue in latest["issues"]:
            if issue["issue_id"] in closed or issue["code"] == "MISSING_SIGNATURE":
                continue
            with st.expander(f"处理差异：{issue['detail']}"):
                with st.form(f"resolve_{issue['issue_id']}"):
                    explanation = st.text_area("处理结论 *", key=f"explain_{issue['issue_id']}")
                    support = st.file_uploader("处理依据图片或 PDF *", type=["pdf", "png", "jpg", "jpeg", "webp"],
                                               key=f"support_{issue['issue_id']}")
                    if st.form_submit_button("记录处理并关闭跟进"):
                        try:
                            if not support:
                                raise ValueError("请上传处理依据")
                            service.resolve_issue(selected, issue["issue_id"], explanation,
                                                  support.name, support.getvalue())
                            st.success("处理依据已保存；原始差异仍可追溯。")
                            st.rerun()
                        except Exception as error:
                            st.error(str(error))
        st.markdown("#### 联系人与专属入口")
        for participant in service.participants(selected):
            role = participant["role"]
            st.write(f"{ROLES[role]}：{participant['name']} · "
                     f"{participant['email'] or participant['phone']}")
            st.code(service.task_link(selected, role) or service.task_link(selected, role, local=True) or "")
            if st.button(f"撤销并重发{ROLES[role]}入口", key=f"rotate_{selected}_{role}"):
                service.invite(selected, role, participant["name"],
                               participant["email"] or "", participant["phone"] or "",
                               contact_allowed=bool(participant["contact_allowed"]))
                st.rerun()
        st.markdown("#### 提醒记录")
        notices = service.notifications(selected)
        if notices:
            status_labels = {"manual": "未发送，需人工转发", "queued": "未发送，等待渠道处理",
                             "sent": "已交给发送服务", "failed": "发送失败", "canceled": "已取消"}
            channel_labels = {"manual": "手动通知", "email": "邮件", "phone": "语音电话"}
            for notice in notices:
                with st.container(border=True):
                    label = ROLES.get(notice["role"], "供应商负责人")
                    state = ("未发送，需人工转发" if notice["channel"] == "manual" and
                             notice["status"] == "queued" else
                             status_labels.get(notice["status"], notice["status"]))
                    st.write(f"{label} · {channel_labels.get(notice['channel'], notice['channel'])}"
                             f" · {state}")
                    display_body = notice["body"].replace("partial", "部分接收").replace("。。", "。")
                    st.caption(display_body)
        else:
            st.caption("目前没有达到提醒时点的任务。")
        with st.expander("查看交付与复核日志"):
            for event in service.events(selected):
                actor = {"agent": "规则巡检", "supplier": "供应商", "driver": "配送人员",
                         "receiver": "收货人"}.get(event["actor_role"], event["actor_role"])
                st.write(f"{event['created_at']} · {actor} · {event['detail']}")


def participant_page(token: str) -> None:
    try:
        person = service.participant(token)
    except PermissionError as error:
        st.error(str(error))
        return
    order = service.order(person["order_id"])
    role = person["role"]
    st.markdown('<section class="hero compact"><div class="eyebrow">专属交付任务</div>'
                f'<h1>{ROLES[role]}工作页</h1><p>仅可处理分配给你的订单 {order.order_id}。</p></section>',
                unsafe_allow_html=True)
    st.write(f"你好，{person['name']}。客户：{order.customer}；商品：{order.items[0].sku}；"
             f"订单数量：{order.items[0].quantity}。")
    assigned = [d for d in service.investigations(order.order_id)
                if d["target_role"] == role and d["status"] == "approved"]
    if assigned:
        st.markdown("#### 需要你补充核实的事项")
        for draft in assigned:
            with st.form(f"investigation_reply_{draft['id']}"):
                st.write(draft["request"])
                reply = st.text_area("请说明实际情况 *", key=f"reply_{draft['id']}")
                attachment = st.file_uploader("现场照片或文件（可选）",
                                              type=["pdf", "png", "jpg", "jpeg", "webp"],
                                              key=f"reply_file_{draft['id']}")
                if st.form_submit_button("提交补证回复"):
                    try:
                        service.respond_to_investigation(token, draft["id"], reply,
                            evidence=(attachment.name, attachment.getvalue()) if attachment else None)
                        st.success("回复已记录。供应商可查看并继续调查。")
                        st.rerun()
                    except Exception as error:
                        st.error(str(error))
    if role == "driver":
        st.markdown("#### 填写本次送货")
        st.caption("请按仓库或实际交付记录填写原始送货单号和实送数量。你不能修改订单价格或客户签收。")
        with st.form("driver_delivery"):
            delivery_id = st.text_input("送货单号 *")
            qty = st.number_input("本次实送数量 *", min_value=0.01, value=None)
            declared_amount = st.number_input("送货单总金额（元）*", min_value=0.0, value=None)
            delivery_proof = st.file_uploader("原始送货单照片或 PDF（可选）",
                                              type=["pdf", "png", "jpg", "jpeg", "webp"])
            submit = st.form_submit_button("提交送货记录", type="primary")
        if submit:
            try:
                if qty is None or declared_amount is None:
                    raise ValueError("请填写实送数量和送货单原始金额")
                service.record_delivery(token, delivery_id.strip(), Decimal(str(qty)),
                    declared_amount=Decimal(str(declared_amount)),
                    evidence=(delivery_proof.name, delivery_proof.getvalue()) if delivery_proof else None)
                service.dispatch_queued()
                st.success("送货记录已提交。系统已重新核对，并等待收货人确认。")
            except Exception as error:
                st.error(str(error))
        st.markdown("#### 我提交的送货单")
        for delivery in service.deliveries(order.order_id):
            st.write(f"{delivery.delivery_id} · {delivery.items[0].quantity} {delivery.items[0].sku}")
    else:
        deliveries = service.deliveries(order.order_id)
        st.markdown("#### 确认实际收到的货")
        if not deliveries:
            st.info("配送方还没有提交送货单；请稍后再打开此任务入口。")
            return
        confirmations = {receipt["delivery_id"] for receipt in service.receipts(order.order_id)}
        open_deliveries = [d for d in deliveries if d.delivery_id not in confirmations]
        if not open_deliveries:
            st.success("当前送货单已有收货反馈。")
            return
        with st.form("receiver_receipt"):
            delivery_id = st.selectbox("要确认的送货单", [d.delivery_id for d in open_deliveries])
            delivery = next(d for d in open_deliveries if d.delivery_id == delivery_id)
            st.caption(f"送货方登记：{delivery.items[0].quantity} {delivery.items[0].sku}")
            condition = st.selectbox("收货情况", ["accepted", "partial", "damaged", "refused"],
                                     format_func=lambda c: {"accepted": "全部接收", "partial": "部分接收",
                                                            "damaged": "有损坏", "refused": "拒收"}[c])
            accepted = st.number_input("实际接收数量", min_value=0.0,
                                       value=float(delivery.items[0].quantity))
            note = st.text_area("差异、损坏或拒收说明")
            proof = st.file_uploader("现场签收照片或 PDF（可选）", type=["pdf", "png", "jpg", "jpeg", "webp"])
            confirmed = st.checkbox("我确认以上是本次实际收货情况；专属链接提交会留下姓名和时间记录")
            submit = st.form_submit_button("提交收货确认", type="primary")
        if submit:
            try:
                if not confirmed:
                    raise ValueError("请确认实际收货情况")
                service.record_receipt(token, delivery_id, Decimal(str(accepted)),
                    condition=condition, note=note,
                    evidence=(proof.name, proof.getvalue()) if proof else None)
                service.dispatch_queued()
                st.success("收货情况已提交。供应商可看到差异和复核记录。")
            except Exception as error:
                st.error(str(error))
    st.caption("此入口仅凭专属链接控制访问，不能单独证明实际使用者身份；如链接泄露请联系供应商重新邀请。")


st.markdown('<div class="app-nav"><div class="app-logo">D</div>'
            '<div class="app-brand">交付协作工作台<small>DELIVERY PROOF / COLLABORATION</small></div></div>'
            '<div class="top-rule"></div>', unsafe_allow_html=True)
token = st.query_params.get("token", "")
if token:
    participant_page(token)
elif st.session_state.get("owner_authenticated"):
    owner_page()
else:
    st.markdown('<section class="hero"><div class="eyebrow">一笔订单 · 多方协作</div>'
                '<h1>从发货到签收，<br><em>每一步有人负责。</em></h1>'
                '<p>供应商建单，配送人员登记送货，收货人确认实收。系统自动核对与提醒；遇到复杂差异时可请 AI 协助调查。</p></section>',
                unsafe_allow_html=True)
    st.markdown("### 供应商登录")
    st.write("供应商先建立订单并邀请相关人员。配送人员和收货人会收到各自的专属任务链接，"
             "打开链接即可填写自己负责的记录，无需供应商口令。")
    if not os.getenv("DELIVERYPROOF_ADMIN_PASSWORD"):
        st.info("首次使用？在运行本项目的电脑上打开项目目录，执行 "
                "`Get-Content .tmp\\owner-password.txt`（Windows）或 "
                "`cat .tmp/owner-password.txt`（macOS/Linux）读取本机口令。"
                "每份安装的口令不同，不会上传到 GitHub。")
    with st.form("owner_login"):
        password = st.text_input("管理员口令", type="password")
        submit = st.form_submit_button("进入工作台", type="primary")
    if submit:
        if hmac.compare_digest(password, owner_password):
            st.session_state["owner_authenticated"] = True
            st.rerun()
        else:
            st.error("口令错误")
