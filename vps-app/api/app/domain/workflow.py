"""申請の操作（Workflow.gs / Pdf.gs の generateRequestPdf の移植）。

関数名・引数の並び・エラー文言・戻り値の形は GAS 版と同じ。変えた点（計画書 9 章の決定）:
- 全画面承認（kiosk）は状態と段を確かめ、さらに expectedStep（最後の追加引数）を今の段と照合する
- 再申請で amountWaived を false に戻す
- 申請日の既定は日本時間の今日
- マイナスの単価をはじく
- 上席承認待ち（一覧とバッジ）は、上席には自分が上席の部署だけ（管理者・総務部長は全部署）
- 一覧はページ送り（{rows, total, page, pageSize, summary}）
- 後回しの仕事は models.Job に積む。詳細は deferredJobs の代わりに pendingJobs（件数）を返す
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import ACTION, CATEGORY_VALUES, STATUS, STEPS
from app.domain import outbox, store
from app.domain.helpers import (
    format_currency,
    gross_amount,
    js_round,
    js_string,
    js_truthy,
    json_parse,
    normalize_email,
    normalize_unit,
    parse_boolean,
    parse_date_input,
    parse_number,
    sanitize_text,
    split_emails,
    tax_amount,
    to_client_request,
)
from app.domain.labels import (
    ACTION_LABELS,
    KIOSK_STEPS,
    PRESIDENT_ROUTE,
    REASONS,
    SPLITTABLE_STEPS,
    STANDARD_ROUTE,
    STATUS_LABELS,
    STEP_LABELS,
)
from app.domain.masters import Masters
from app.models import History, Request, RequestItem
from app.rpc import RpcContext, RpcError
from app.serialize import history_row, item_row, request_row
from app.timeutil import today_jst

MAX_PAGE_SIZE = 200
ERR_REOPEN = "画面を開いた後に申請の段が変わりました。画面を開き直してください。"


class Op:
    """1 回の呼び出しの間の道具（利用者・マスタ・設定を 1 回だけ読む）。"""

    def __init__(self, ctx: RpcContext):
        self.ctx = ctx
        self.db: Session = ctx.db
        self.email: str = ctx.user.email
        self.name: str = ctx.user.name
        self._masters: Masters | None = None
        self._settings: dict[str, str] | None = None

    @property
    def masters(self) -> Masters:
        if self._masters is None:
            self._masters = Masters.load(self.db)
        return self._masters

    @property
    def settings(self) -> dict[str, str]:
        if self._settings is None:
            self._settings = store.load_settings(self.db)
        return self._settings

    @property
    def admin(self) -> bool:
        return self.ctx.is_admin

    @property
    def user(self) -> dict[str, str]:
        return {"email": self.email, "name": self.name}

    def reload_masters(self) -> None:
        self._masters = None
        self._settings = None


# ===== 権限の判定（Workflow.gs） =====


def is_current_approver(request: Request, email: str) -> bool:
    target = normalize_email(email)
    if not target:
        return False
    return target in split_emails(request.current_approver_email)


def is_president_pending_row(request: Request) -> bool:
    return request.status == STATUS.IN_REVIEW and request.current_step == STEPS.PRESIDENT


def can_approve(op: Op, request: Request) -> bool:
    if request.status != STATUS.IN_REVIEW:
        return False
    if op.admin:
        return True
    if is_current_approver(request, op.email):
        return True
    return op.masters.is_step_approver_for(op.email, request.applicant_email, request.department, request.current_step)


def can_president(op: Op, request: Request) -> bool:
    return is_president_pending_row(request) and (
        op.admin
        or is_current_approver(request, op.email)
        or op.masters.is_step_approver_for(op.email, request.applicant_email, request.department, STEPS.PRESIDENT)
    )


def can_edit(op: Op, request: Request) -> bool:
    return request.status == STATUS.RETURNED and bool(op.email)


can_resubmit = can_edit
can_delete_pool = can_edit


def can_edit_in_review(op: Op, request: Request) -> bool:
    return (
        bool(op.email)
        and request.status == STATUS.IN_REVIEW
        and not bool(request.amount_waived)
        and request.current_step in (STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE)
    )


def is_involved(op: Op, request: Request) -> bool:
    if (
        normalize_email(request.applicant_email) == op.email
        or is_current_approver(request, op.email)
        or op.admin
    ):
        return True
    if op.masters.is_step_approver_for(op.email, request.applicant_email, request.department, request.current_step):
        return True
    actors = op.db.execute(select(History.actor_email).where(History.request_id == request.request_id)).scalars()
    return any(normalize_email(a) == op.email for a in actors)


def can_generate_pdf(op: Op, request: Request) -> bool:
    return request.status == STATUS.COMPLETED and is_involved(op, request)


def can_delete(op: Op, request: Request) -> bool:
    if request.status == STATUS.CANCELLED:
        return False
    if op.admin:
        return True
    if request.status == STATUS.RETURNED and op.email:
        return True
    return normalize_email(request.applicant_email) == op.email and request.status != STATUS.COMPLETED


def last_advance_actor_email(db: Session, request_id: str, current_step: str) -> str:
    forward = (ACTION.SUBMIT, ACTION.RESUBMIT, ACTION.APPROVE, ACTION.QUOTE, ACTION.EXPEDITE)
    rows = [h for h in store.get_history(db, request_id) if h.to_step == current_step and h.action in forward]
    return normalize_email(rows[-1].actor_email) if rows else ""


def can_recall(op: Op, request: Request) -> bool:
    if request.status != STATUS.IN_REVIEW:
        return False
    route = json_parse(request.route_json, [])
    current_index = route.index(request.current_step) if request.current_step in route else -1
    if current_index <= 0:
        return False
    if can_approve(op, request):
        return True
    advancer = last_advance_actor_email(op.db, request.request_id, request.current_step)
    return bool(advancer) and advancer == op.email


def can_split_items(op: Op, request: Request, item_count: Any) -> bool:
    if request.status != STATUS.IN_REVIEW:
        return False
    if request.current_step not in SPLITTABLE_STEPS:
        return False
    if not (parse_number(item_count) > 1):
        return False
    return can_approve(op, request) or can_president(op, request)


def assert_kiosk_target(request: Request) -> None:
    if request.status != STATUS.IN_REVIEW or request.current_step not in KIOSK_STEPS:
        raise RpcError("全画面モードで操作できる申請ではありません。画面を開き直してください。")


def assert_expected_step(request: Request, expected_step: Any) -> None:
    """VPS 版で追加: 画面を開いたときの段（expectedStep）と今の段が違えば止める（2 台で同時に押したとき）。"""
    expected = sanitize_text(expected_step, 20)
    if expected and expected != request.current_step:
        raise RpcError(ERR_REOPEN)


def kiosk_attribution(request: Request, op: Op, comment: Any) -> dict[str, str]:
    """全画面モードの代理記録: 印と履歴は今の段の正規の承認者、操作した人は注記で残す。"""
    note = "（" + op.name + "の端末で代理記録）"
    return {
        "email": request.current_approver_email or op.email,
        "name": request.current_approver_name or op.name,
        "comment": " ".join(p for p in (sanitize_text(comment, 1000), note) if p),
    }


# ===== 入力の整え方 =====


def _item_inputs(payload: Any) -> list[dict[str, Any]]:
    data = payload if isinstance(payload, dict) else {}
    items = data.get("items") or []
    if not isinstance(items, list):
        return []
    return [item if isinstance(item, dict) else {} for item in items]


def _validate_items(items: list[dict[str, Any]]) -> None:
    if not items:
        raise RpcError("明細を1行以上入力してください。")
    for index, item in enumerate(items, start=1):
        if not item["name"]:
            raise RpcError(f"明細{index}行目の品名を入力してください。")
        if item["quantity"] <= 0:
            raise RpcError(f"明細{index}行目の数量を1以上にしてください。")
        if parse_number(item["unitPrice"]) < 0:
            raise RpcError(f"明細{index}行目の単価は0以上で入力してください。")
        try:
            parse_date_input(item["desiredDeliveryDate"])
        except ValueError:
            raise RpcError(f"明細{index}行目の希望納期は日付（yyyy-MM-dd）で入力してください。") from None


def _keep_item(item: dict[str, Any]) -> bool:
    return bool(item["name"] or item["model"] or item["maker"] or js_truthy(item["quantity"]) or item["note"])


def normalize_request_payload(payload: Any, op: Op) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    # 決定事項: 既定は日本時間の今日（GAS 版の朝 9 時前は前日になる癖は直す）
    request_date = sanitize_text(data.get("requestDate"), 20) or today_jst().strftime("%Y-%m-%d")
    department = sanitize_text(data.get("department"), 100)
    category = sanitize_text(data.get("category"), 20)
    reason_code = sanitize_text(data.get("reasonCode"), 1)

    if not department:
        raise RpcError("部署を入力してください。")
    if category not in CATEGORY_VALUES:
        raise RpcError("品目区分（メカ／電気／一般・不明）を選択してください。")
    if not any(r["code"] == reason_code for r in REASONS):
        raise RpcError("購入理由A-Fを選択してください。")

    items = []
    for item in _item_inputs(data):
        items.append({
            "name": sanitize_text(item.get("name"), 200),
            "model": sanitize_text(item.get("model"), 200),
            "maker": sanitize_text(item.get("maker"), 200),
            "quantity": parse_number(item.get("quantity")),
            "unit": normalize_unit(item.get("unit")),
            "unitPrice": parse_number(item.get("unitPrice")),
            "amount": 0,
            "desiredDeliveryDate": sanitize_text(item.get("desiredDeliveryDate"), 20),
            "note": sanitize_text(item.get("note"), 500),
        })
    items = [item for item in items if _keep_item(item)]
    _validate_items(items)
    try:
        parsed_date = parse_date_input(request_date)
    except ValueError:
        raise RpcError("申請日は日付（yyyy-MM-dd）で入力してください。") from None

    return {
        "applicantName": sanitize_text(data.get("applicantName"), 100) or op.name,
        "department": department,
        "category": category,
        "requestDate": parsed_date,
        "reasonCode": reason_code,
        "reasonDetail": sanitize_text(data.get("reasonDetail"), 2000),
        "items": items,
        "totalAmount": 0,
    }


def normalize_editable_items(payload: Any) -> list[dict[str, Any]]:
    items = []
    for item in _item_inputs(payload):
        items.append({
            "name": sanitize_text(item.get("name"), 200),
            "model": sanitize_text(item.get("model"), 200),
            "maker": sanitize_text(item.get("maker"), 200),
            "quantity": parse_number(item.get("quantity")),
            "unit": normalize_unit(item.get("unit")),
            # 申請者は単価・金額を扱わない。送られてきても 0 にする。
            "unitPrice": 0,
            "amount": 0,
            "desiredDeliveryDate": sanitize_text(item.get("desiredDeliveryDate"), 20),
            "note": sanitize_text(item.get("note"), 500),
        })
    items = [item for item in items if _keep_item(item)]
    _validate_items(items)
    return items


ITEM_CHANGE_FIELDS = (
    ("name", "品名"),
    ("model", "型式"),
    ("maker", "メーカー"),
    ("quantity", "数量"),
    ("unit", "単位"),
    ("desiredDeliveryDate", "希望納期"),
    ("note", "備考"),
)


def _compare_value(value: Any) -> str:
    return js_string(value).strip()


def describe_item_changes(old_items: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> str:
    lines = []
    for i in range(max(len(old_items), len(new_items))):
        old = old_items[i] if i < len(old_items) else None
        new = new_items[i] if i < len(new_items) else None
        if old is None and new is not None:
            lines.append(f"明細{i + 1}: 追加（{new['name'] or '—'}）")
            continue
        if old is not None and new is None:
            lines.append(f"明細{i + 1}: 削除（{old['name'] or '—'}）")
            continue
        changes = []
        for key, label in ITEM_CHANGE_FIELDS:
            before = _compare_value(old.get(key))
            after = _compare_value(new.get(key))
            if before != after:
                changes.append(f"{label}: {before or '—'}→{after or '—'}")
        if changes:
            lines.append(f"明細{i + 1} " + "、".join(changes))
    return " ／ ".join(lines)


def build_route(op: Op, approver_rule: dict[str, Any]) -> list[str]:
    """buildRoute_: 申請時は 上席→購買(見積)→総務部長→購買(手配)。上席以外の承認者が登録済みか確かめる。"""
    route = list(STANDARD_ROUTE)
    for step in route:
        if step == STEPS.SUPERVISOR:
            continue
        op.masters.get_approver_for_step(approver_rule, step)
    return route


def _first_approvers(op: Op, applicant_email: str, department: str, first_step: str, rule: dict[str, Any]):
    """先頭の段の承認者（代表）と、承認依頼を送る相手。"""
    if first_step == STEPS.SUPERVISOR:
        supervisors = op.masters.require_supervisors(applicant_email, department)
        return {"email": supervisors[0]["email"], "name": supervisors[0]["name"]}, supervisors
    first = op.masters.get_approver_for_step(rule, first_step)
    return first, [first]


# ===== 表示用の組み立て =====


def history_client_rows(rows: list[History]) -> list[dict[str, Any]]:
    result = []
    for h in rows:
        row = history_row(h)
        row["actionLabel"] = ACTION_LABELS.get(row["action"], row["action"])
        row["fromStepLabel"] = STEP_LABELS.get(row["fromStep"], row["fromStep"])
        row["toStepLabel"] = STEP_LABELS.get(row["toStep"], row["toStep"])
        row["fromStatusLabel"] = STATUS_LABELS.get(row["fromStatus"], row["fromStatus"])
        row["toStatusLabel"] = STATUS_LABELS.get(row["toStatus"], row["toStatus"])
        result.append(row)
    return result


def resolve_step_titles(op: Op, request: Request, history: list[dict[str, Any]]) -> dict[str, str]:
    titles: dict[str, str] = {}
    rule = op.masters.find_approver_rule(request.applicant_email, request.department)
    if rule:
        titles[STEPS.GENERAL_MANAGER] = rule["generalManagerTitle"] or ""
        titles[STEPS.PRESIDENT] = rule["presidentTitle"] or ""
        titles[STEPS.PURCHASING] = rule["purchasingTitle"] or ""
    sup_email = ""
    for h in reversed(history):
        if h["fromStep"] == STEPS.SUPERVISOR and h["action"] in (ACTION.APPROVE, ACTION.RETURN):
            sup_email = h["actorEmail"]
            break
    if not sup_email and request.current_step == STEPS.SUPERVISOR:
        sup_email = request.current_approver_email
    sup_title = op.masters.supervisor_title_by_email(sup_email, request.applicant_email, request.department) if sup_email else ""
    titles[STEPS.SUPERVISOR] = sup_title or ((rule["supervisorTitle"] or "") if rule else "")
    return titles


def stamp_names(op: Op, request: Request, history: list[dict[str, Any]]) -> dict[str, str]:
    """VPS 版で追加（決定事項 Q3: 押印は氏名）: 履歴の操作者メール → 上席マスタ・承認者マスタの氏名。

    マスタに無い人は入れない（画面・PDF は履歴の操作者名に戻す）。
    """
    names: dict[str, str] = {}
    for h in history:
        email = normalize_email(h["actorEmail"])
        if not email or email in names:
            continue
        full = op.masters.full_name_for(email, request.applicant_email, request.department)
        if full:
            names[email] = full
    return {k: v for k, v in names.items() if v}


def build_detail(op: Op, request: Request) -> dict[str, Any]:
    """getRequestDetail の戻り値。"""
    if not op.email:
        raise RpcError("この申請を閲覧する権限がありません。")
    items = store.get_item_rows(op.db, request.request_id)
    history = history_client_rows(store.get_history(op.db, request.request_id))
    approve = can_approve(op, request)
    return {
        "request": to_client_request(request),
        "items": items,
        "history": history,
        "permissions": {
            "canApprove": approve,
            "canReturn": approve,
            "canDelete": can_delete(op, request),
            "canEdit": can_edit(op, request),
            "canResubmit": can_resubmit(op, request),
            "canDeletePool": can_delete_pool(op, request),
            "canEditInReview": can_edit_in_review(op, request),
            "canGeneratePdf": can_generate_pdf(op, request),
            "canPresident": can_president(op, request),
            "canTabletApprove": approve,
            "canQuote": approve and request.current_step == STEPS.PURCHASING_QUOTE,
            "canArrange": approve and request.current_step == STEPS.PURCHASING,
            "canRecall": can_recall(op, request),
            "canSplitItems": can_split_items(op, request, len(items)),
        },
        "stepTitles": resolve_step_titles(op, request, history),
        "purchasingStampName": op.masters.purchasing_stamp_name(request.applicant_email, request.department),
        "stampNames": stamp_names(op, request, history),
        # GAS 版の deferredJobs の代わり。後回しの仕事（PDF・メール）のうち、まだ終わっていない数。
        "pendingJobs": outbox.pending_count(op.db, request.request_id),
    }


def _detail(op: Op, request_id: str) -> dict[str, Any]:
    op.db.flush()
    request = store.require_request(op.db, request_id)
    op.db.refresh(request)
    return build_detail(op, request)


def _snapshot(request: Request) -> dict[str, Any]:
    """後回しの仕事に渡す申請の写し（serialize.request_row の形）。"""
    return request_row(request)


# ===== 一覧・件数 =====


def _matches_mode(op: Op, request: Request, mode: str, supervisor_all: bool) -> bool:
    if request.status == STATUS.CANCELLED and mode not in ("mine", "all"):
        return False
    if mode == "pending":
        if is_current_approver(request, op.email):
            return True
        if request.status == STATUS.IN_REVIEW and op.masters.is_step_approver_for(
            op.email, request.applicant_email, request.department, request.current_step
        ):
            return True
        return op.admin and is_president_pending_row(request)
    if mode == "supervisor":
        if not (request.status == STATUS.IN_REVIEW and request.current_step == STEPS.SUPERVISOR):
            return False
        # 決定事項: 上席には自分が上席の部署だけ。管理者・総務部長は全部署。
        return supervisor_all or op.masters.is_supervisor_for(op.email, request.applicant_email, request.department)
    if mode == "quote":
        return request.current_step == STEPS.PURCHASING_QUOTE
    if mode == "gm":
        return request.status == STATUS.IN_REVIEW and request.current_step == STEPS.GENERAL_MANAGER
    if mode == "president":
        return is_president_pending_row(request)
    if mode == "arrange":
        return request.status == STATUS.IN_REVIEW and request.current_step == STEPS.PURCHASING
    if mode == "returned":
        return request.status == STATUS.RETURNED
    if mode == "all":
        return True
    return normalize_email(request.applicant_email) == op.email


def sees_all_supervisor_rows(op: Op) -> bool:
    return op.admin or op.masters.is_general_manager_anywhere(op.email)


def _int_arg(value: Any, default: int) -> int:
    number = parse_number(value)
    return int(number) if number and float(number).is_integer() else default


def get_requests(ctx: RpcContext, filter: Any = None) -> dict[str, Any]:  # noqa: A002 - GAS 版と同じ名前
    op = Op(ctx)
    data = filter if isinstance(filter, dict) else {}
    mode = js_string(data.get("mode")) or "mine"
    query = sanitize_text(data.get("query"), 100).lower()
    status = sanitize_text(data.get("status"), 50)
    page = max(1, _int_arg(data.get("page"), 1))
    page_size = min(MAX_PAGE_SIZE, max(1, _int_arg(data.get("pageSize"), MAX_PAGE_SIZE)))
    supervisor_all = sees_all_supervisor_rows(op) if mode == "supervisor" else False

    rows = []
    for request in op.db.execute(select(Request)).scalars():
        if not _matches_mode(op, request, mode, supervisor_all):
            continue
        if status and request.status != status:
            continue
        if query:
            haystack = " ".join([
                request.request_id, request.applicant_name, request.department,
                request.reason_detail, request.current_approver_name,
            ]).lower()
            if query not in haystack:
                continue
        rows.append(request)

    # 並びは更新日時の新しい順（決定事項 Q8: 時刻まで使う）。同じ時刻は申請 ID の新しい順。
    rows.sort(key=lambda r: (r.updated_at.timestamp() if r.updated_at else float("-inf"), r.request_id), reverse=True)

    in_review = sum(1 for r in rows if r.status == STATUS.IN_REVIEW)
    returned = sum(1 for r in rows if r.status == STATUS.RETURNED)
    # 未完了金額: 完了・取消を除く。1 件ずつ税込（税は切り捨て）にしてから足す。
    open_amount = sum(
        gross_amount(r.total_amount) for r in rows if r.status not in (STATUS.COMPLETED, STATUS.CANCELLED)
    )
    start = (page - 1) * page_size
    return {
        "rows": [to_client_request(r) for r in rows[start:start + page_size]],
        "total": len(rows),
        "page": page,
        "pageSize": page_size,
        "summary": {"inReview": in_review, "returned": returned, "openAmount": open_amount},
    }


def get_tab_counts(ctx: RpcContext) -> dict[str, int]:
    op = Op(ctx)
    counts = {"pending": 0, "president": 0, "quote": 0, "supervisor": 0, "gm": 0, "arrange": 0, "returned": 0}
    supervisor_all = sees_all_supervisor_rows(op)
    for request in op.db.execute(select(Request)).scalars():
        if request.status == STATUS.CANCELLED:
            continue
        if request.status == STATUS.RETURNED:
            counts["returned"] += 1
        in_review = request.status == STATUS.IN_REVIEW
        is_pres = is_president_pending_row(request)
        if in_review and request.current_step == STEPS.SUPERVISOR and (
            supervisor_all or op.masters.is_supervisor_for(op.email, request.applicant_email, request.department)
        ):
            counts["supervisor"] += 1
        if request.current_step == STEPS.PURCHASING_QUOTE:
            counts["quote"] += 1
        if in_review and request.current_step == STEPS.GENERAL_MANAGER:
            counts["gm"] += 1
        if is_pres:
            counts["president"] += 1
        if in_review and request.current_step == STEPS.PURCHASING:
            counts["arrange"] += 1
        mine_approve = is_current_approver(request, op.email)
        mine_member = (in_review or is_pres) and op.masters.is_step_approver_for(
            op.email, request.applicant_email, request.department, request.current_step
        )
        if mine_approve or mine_member or (op.admin and is_pres):
            counts["pending"] += 1
    return counts


def get_request_detail(ctx: RpcContext, requestId: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId)
    return build_detail(op, request)


# ===== 申請・修正・再申請 =====


def create_request(ctx: RpcContext, payload: Any = None) -> dict[str, Any]:
    op = Op(ctx)
    normalized = normalize_request_payload(payload, op)
    rule = op.masters.require_approver_rule(op.email, normalized["department"])
    route = build_route(op, rule)
    first_step = route[0]
    first_approver, notify = _first_approvers(op, op.email, normalized["department"], first_step, rule)
    now = store.now()
    request_id = store.unique_id(op.db, Request, "REQ")

    request = Request(
        request_id=request_id,
        created_at=now,
        updated_at=now,
        submitted_at=now,
        completed_at=None,
        applicant_email=op.email,
        applicant_name=normalized["applicantName"],
        department=normalized["department"],
        request_date=normalized["requestDate"],
        reason_code=normalized["reasonCode"],
        reason_detail=normalized["reasonDetail"],
        total_amount=0,
        category=normalized["category"],
        status=STATUS.IN_REVIEW,
        current_step=first_step,
        current_approver_email=first_approver["email"],
        current_approver_name=first_approver["name"],
        route_json=_route_json(route),
        pdf_file_id="",
        pdf_url="",
        version="1.0.0",
        amount_waived=False,
        split_from_request_id="",
        row_version=1,
    )
    op.db.add(request)
    op.db.flush()
    store.replace_items(op.db, request_id, normalized["items"])
    store.add_history(
        op.db, request_id=request_id, at=now, actor_email=op.email, actor_name=normalized["applicantName"],
        action=ACTION.SUBMIT, to_status=STATUS.IN_REVIEW, to_step=first_step,
    )
    snapshot = _snapshot(request)
    for member in notify:
        outbox.defer(op.db, "sendApprovalRequestEmail_", [snapshot, member], guard=request)
    return _detail(op, request_id)


def _route_json(route: list[str]) -> str:
    import json

    return json.dumps(route, ensure_ascii=False, separators=(",", ":"))


def update_request_draft(ctx: RpcContext, requestId: Any = None, payload: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if request.status != STATUS.RETURNED:
        raise RpcError("差戻し中の申請のみ修正できます。")
    normalized = normalize_request_payload(payload, op)
    now = store.now()
    store.touch(request, now)
    request.applicant_name = normalized["applicantName"]
    request.department = normalized["department"]
    request.request_date = normalized["requestDate"]
    request.reason_code = normalized["reasonCode"]
    request.reason_detail = normalized["reasonDetail"]
    request.total_amount = 0
    request.category = normalized["category"]
    store.replace_items(op.db, request.request_id, normalized["items"])
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.UPDATE, from_status=request.status, to_status=request.status,
        from_step=request.current_step, to_step=request.current_step, comment="申請内容を修正",
    )
    return _detail(op, request.request_id)


def update_request_in_review(ctx: RpcContext, requestId: Any = None, payload: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_edit_in_review(op, request):
        raise RpcError("この申請は現在修正できません。承認中かつ購買が金額を入力する前の申請のみ、明細を修正できます。")
    old_items = store.get_item_rows(op.db, request.request_id)
    new_items = normalize_editable_items(payload)
    summary = describe_item_changes(old_items, new_items)
    if not summary:
        return _detail(op, request.request_id)

    now = store.now()
    store.replace_items(op.db, request.request_id, new_items)
    store.touch(request, now)
    request.total_amount = 0
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.UPDATE, from_status=request.status, to_status=request.status,
        from_step=request.current_step, to_step=request.current_step, comment=summary,
    )
    outbox.defer(op.db, "sendRequestUpdatedEmail_", [_snapshot(request), summary, op.name, op.email], guard=request)
    return _detail(op, request.request_id)


def resubmit_request(ctx: RpcContext, requestId: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if request.status != STATUS.RETURNED:
        raise RpcError("差戻し中の申請のみ再申請できます。")
    rule = op.masters.require_approver_rule(request.applicant_email, request.department)
    route = build_route(op, rule)
    first_step = route[0]
    first_approver, notify = _first_approvers(op, request.applicant_email, request.department, first_step, rule)
    from_status, from_step = request.status, request.current_step
    now = store.now()
    store.touch(request, now)
    request.submitted_at = now
    request.total_amount = 0
    request.status = STATUS.IN_REVIEW
    request.current_step = first_step
    request.current_approver_email = first_approver["email"]
    request.current_approver_name = first_approver["name"]
    request.route_json = _route_json(route)
    request.pdf_file_id = ""  # 参照だけ外す（ドライブのファイルは消さない）
    request.pdf_url = ""
    request.amount_waived = False  # 決定事項: 再申請で「金額不要（至急）」を元に戻す
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.RESUBMIT, from_status=from_status, to_status=STATUS.IN_REVIEW,
        from_step=from_step, to_step=first_step,
    )
    snapshot = _snapshot(request)
    for member in notify:
        outbox.defer(op.db, "sendApprovalRequestEmail_", [snapshot, member], guard=request)
    return _detail(op, request.request_id)


# ===== 明細の部分差戻し（申請の分割） =====


def resolve_split_selection(op: Op, request: Request, refs: list[Any]) -> dict[str, list[RequestItem]]:
    """itemId・lineNo・品名の 3 点で画面の明細と照合し、移す明細と残す明細に分ける（書き込みはしない）。"""
    items = store.get_items(op.db, request.request_id)
    claimed: set[int] = set()
    moved: list[RequestItem] = []
    for ref in refs or []:
        ref = ref if isinstance(ref, dict) else {}
        matched = None
        for i, item in enumerate(items):
            if i in claimed:
                continue
            row = item_row(item)
            if (
                js_string(row["itemId"]) == js_string(ref.get("itemId"))
                and js_string(row["lineNo"]) == js_string(ref.get("lineNo"))
                and js_string(row["name"]) == js_string(ref.get("name"))
            ):
                claimed.add(i)
                matched = item
                break
        if matched is None:
            raise RpcError("明細の情報が画面と一致しません。他の人が明細を更新した可能性があります。画面を再読込してからやり直してください。")
        moved.append(matched)
    if not moved:
        raise RpcError("差し戻す明細を選択してください。")
    remaining = [item for i, item in enumerate(items) if i not in claimed]
    if not remaining:
        raise RpcError("すべての明細を差し戻す場合は、申請全体の差戻しをご利用ください。")
    moved.sort(key=lambda item: item.line_no)
    return {"moved": moved, "remaining": remaining}


def assert_split_allowed(op: Op, request: Request, kiosk: Any, clean_comment: str) -> None:
    if js_truthy(kiosk):
        raise RpcError("全画面モードでは明細ごとの差戻しはできません。詳細画面から操作してください。")
    if not can_split_items(op, request, len(store.get_items(op.db, request.request_id))):
        raise RpcError("この申請では明細ごとの差戻しはできません。")
    if not clean_comment:
        raise RpcError("差し戻す明細がある場合は、理由（コメント）を入力してください。")


def create_returned_split_request(
    op: Op, request: Request, moved: list[RequestItem], comment: str, now: datetime
) -> str:
    """分離先の申請（差戻し）を作り、明細を移し、履歴を 2 行入れる。戻り値は新しい申請 ID。"""
    new_id = store.unique_id(op.db, Request, "REQ")
    split = Request(
        request_id=new_id,
        created_at=now,
        updated_at=now,
        submitted_at=request.submitted_at,
        completed_at=None,
        applicant_email=request.applicant_email,
        applicant_name=request.applicant_name,
        department=request.department,
        request_date=request.request_date,
        reason_code=request.reason_code,
        reason_detail=request.reason_detail,
        total_amount=0,
        status=STATUS.RETURNED,
        current_step=STEPS.APPLICANT,
        current_approver_email="",
        current_approver_name="",
        route_json=_route_json(STANDARD_ROUTE),
        pdf_file_id="",
        pdf_url="",
        version="1.0.0",
        amount_waived=False,
        category=request.category,
        split_from_request_id=request.request_id,
        row_version=1,
    )
    op.db.add(split)
    op.db.flush()
    store.move_items(op.db, moved, new_id)
    request_date_text = request.request_date.strftime("%Y-%m-%d") if request.request_date else ""
    store.add_history(
        op.db, request_id=new_id, at=now, actor_email=request.applicant_email, actor_name=request.applicant_name,
        action=ACTION.SUBMIT, to_status=STATUS.IN_REVIEW, from_step=STEPS.APPLICANT, to_step=STEPS.SUPERVISOR,
        comment=f"{request.request_id} から分離（元の申請日 {request_date_text}）",
    )
    store.add_history(
        op.db, request_id=new_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.RETURN, from_status=request.status, to_status=STATUS.RETURNED,
        from_step=request.current_step, to_step=STEPS.APPLICANT,
        comment=f"{comment}（{request.request_id} の明細から分離）",
    )
    return new_id


def add_split_out_history(
    op: Op, request: Request, moved: list[RequestItem], new_id: str, clean_comment: str, now: datetime
) -> None:
    head = moved[0].name if moved else ""
    label = f"明細「{head}」ほか計{len(moved)}件" if len(moved) > 1 else f"明細「{head}」"
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.SPLIT_OUT, from_status=request.status, to_status=request.status,
        from_step=request.current_step, to_step=request.current_step,
        comment=f"{label}を {new_id} として差し戻し: {clean_comment}",
    )


def _clone_item(item: RequestItem) -> dict[str, Any]:
    row = item_row(item)
    return {
        "itemId": row["itemId"],
        "name": row["name"],
        "model": row["model"],
        "maker": row["maker"],
        "quantity": parse_number(row["quantity"]),
        "unit": row["unit"],
        "unitPrice": parse_number(row["unitPrice"]),
        "amount": parse_number(row["amount"]),
        "desiredDeliveryDate": row["desiredDeliveryDate"],
        "note": row["note"],
    }


def split_items_on_approval(
    op: Op, request: Request, return_items: list[Any], clean_comment: str, kiosk: Any, now: datetime
) -> str:
    """承認と同時に行う分割（approveRequest / recordPresidentDecision）。分離先の作成 → 残明細 → 合計 → 履歴 → 通知。"""
    assert_split_allowed(op, request, kiosk, clean_comment)
    selection = resolve_split_selection(op, request, return_items)
    remaining = [_clone_item(item) for item in selection["remaining"]]
    new_id = create_returned_split_request(op, request, selection["moved"], clean_comment, now)
    store.replace_items(op.db, request.request_id, remaining)
    total = sum(parse_number(item["amount"]) for item in remaining)
    store.touch(request, now)
    request.total_amount = total
    add_split_out_history(op, request, selection["moved"], new_id, clean_comment, now)
    outbox.defer(op.db, "notifySplitReturned_", [new_id, request.current_step, clean_comment, op.name], request_id=new_id)
    return new_id


# ===== 承認・差戻し・見積・至急・手配 =====


def _apply_transition(request: Request, next_step: str, next_approver: dict[str, str], now: datetime) -> None:
    store.touch(request, now)
    if next_step == STEPS.DONE:
        request.status = STATUS.COMPLETED
        request.current_step = STEPS.DONE
        request.current_approver_email = ""
        request.current_approver_name = ""
        request.completed_at = now
    else:
        request.status = STATUS.IN_REVIEW
        request.current_step = next_step
        request.current_approver_email = next_approver["email"]
        request.current_approver_name = next_approver["name"]


def approve_request(
    ctx: RpcContext, requestId: Any = None, comment: Any = "", kiosk: Any = False,  # noqa: N803
    returnItems: Any = None, expectedStep: Any = None,  # noqa: N803
) -> dict[str, Any]:
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    assert_expected_step(request, expectedStep)
    if js_truthy(kiosk):
        assert_kiosk_target(request)
    elif not can_approve(op, request):
        raise RpcError("現在の承認者のみ承認できます。")
    if request.current_step == STEPS.PURCHASING_QUOTE:
        raise RpcError("購買（見積）ステップでは見積金額を入力してください。")
    if request.current_step == STEPS.PURCHASING:
        raise RpcError("購買ステップでは手配完了を実行してください。")

    route = json_parse(request.route_json, [])
    if request.current_step not in route:
        raise RpcError("承認経路が不正です。管理者に確認してください。")
    cur_key = request.current_step
    index = route.index(cur_key)
    next_step = route[index + 1] if index + 1 < len(route) else STEPS.DONE
    next_approver: dict[str, str] = {"email": "", "name": ""}
    if next_step != STEPS.DONE:
        rule = op.masters.require_approver_rule(request.applicant_email, request.department)
        next_approver = op.masters.get_approver_for_step(rule, next_step, request.category)

    now = store.now()
    split_happened = False
    if isinstance(returnItems, list) and len(returnItems) > 0:
        split_items_on_approval(op, request, returnItems, sanitize_text(comment, 1000), kiosk, now)
        split_happened = True

    act_email, act_name, cmt = op.email, op.name, sanitize_text(comment, 1000)
    if js_truthy(kiosk):
        ka = kiosk_attribution(request, op, comment)
        act_email, act_name, cmt = ka["email"], ka["name"], ka["comment"]

    from_status, from_step = request.status, request.current_step
    _apply_transition(request, next_step, next_approver, now)
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=act_email, actor_name=act_name,
        action=ACTION.COMPLETE if next_step == STEPS.DONE else ACTION.APPROVE,
        from_status=from_status, to_status=request.status, from_step=from_step, to_step=request.current_step,
        comment=cmt,
    )
    snapshot = _snapshot(request)
    outbox.defer(op.db, "dispatchApprovalNotifications_", [snapshot, cur_key, next_step, next_approver, op.user],
                 guard=request)
    if split_happened:
        outbox.defer(op.db, "regeneratePdfAfterSplit_", [snapshot, next_step, op.user], guard=request)
    return _detail(op, request.request_id)


def confirm_quote(
    ctx: RpcContext, requestId: Any = None, items: Any = None, comment: Any = "", returnItems: Any = None,  # noqa: N803
) -> dict[str, Any]:
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_approve(op, request) or request.current_step != STEPS.PURCHASING_QUOTE:
        raise RpcError("購買担当者のみ見積金額を入力できます。")

    clean_comment = sanitize_text(comment, 1000)
    selection = None
    if isinstance(returnItems, list) and len(returnItems) > 0:
        assert_split_allowed(op, request, False, clean_comment)
        selection = resolve_split_selection(op, request, returnItems)

    price_map: dict[str, float] = {}
    for entry in items if isinstance(items, list) else []:
        if isinstance(entry, dict) and js_truthy(entry.get("itemId")):
            price_map[js_string(entry.get("itemId"))] = parse_number(entry.get("unitPrice"))

    existing = selection["remaining"] if selection else store.get_items(op.db, request.request_id)
    total = 0
    rebuilt = []
    for line_no, item in enumerate(existing, start=1):
        base = _clone_item(item)
        unit_price = price_map[item.item_id] if item.item_id in price_map else base["unitPrice"]
        if unit_price < 0:
            raise RpcError(f"明細{line_no}行目の単価は0以上で入力してください。")
        amount = js_round(base["quantity"] * unit_price)
        total += amount
        base["unitPrice"] = unit_price
        base["amount"] = amount
        rebuilt.append(base)

    if not (total > 0):
        raise RpcError("金額を入力してください。")

    rule = op.masters.require_approver_rule(request.applicant_email, request.department)
    gm = op.masters.get_approver_for_step(rule, STEPS.GENERAL_MANAGER)

    now = store.now()
    split_request_id = ""
    if selection:
        split_request_id = create_returned_split_request(op, request, selection["moved"], clean_comment, now)
    store.replace_items(op.db, request.request_id, rebuilt)
    if selection:
        add_split_out_history(op, request, selection["moved"], split_request_id, clean_comment, now)

    threshold = store.threshold_amount(op.settings)
    over = total >= threshold
    tax = tax_amount(total)
    gross = gross_amount(total)
    amount_summary = f"税抜 {format_currency(total)}（消費税 {format_currency(tax)}／税込 {format_currency(gross)}）"
    new_route = PRESIDENT_ROUTE if over else STANDARD_ROUTE

    store.touch(request, now)
    request.total_amount = total
    request.route_json = _route_json(new_route)
    request.status = STATUS.IN_REVIEW
    request.current_step = STEPS.GENERAL_MANAGER
    request.current_approver_email = gm["email"]
    request.current_approver_name = gm["name"]
    history_comment = " ".join(p for p in ("見積金額 " + amount_summary + ("（税抜10万円以上）" if over else ""),
                                           clean_comment) if p)
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.QUOTE, from_status=STATUS.IN_REVIEW, to_status=STATUS.IN_REVIEW,
        from_step=STEPS.PURCHASING_QUOTE, to_step=STEPS.GENERAL_MANAGER, comment=history_comment,
    )

    snapshot = _snapshot(request)
    over_banner = "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🚨🚨🚨  要・社長決裁  🚨🚨🚨",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"確定金額（税抜）{format_currency(total)} は社長決裁しきい値（税抜 {format_currency(threshold)}）以上です。",
        "総務部長の承認後、必ず【社長の承認】が必要です。至急ご対応ください。",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ])
    if over:
        ga_subject = f"【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 {request.request_id}（税抜10万円以上）"
        ga_heading = over_banner + "\n\n購買が見積金額 " + amount_summary + " を確定しました。"
    else:
        ga_subject = f"[貯蔵品購入申請] 金額確定 {request.request_id}"
        ga_heading = "購買が見積金額 " + amount_summary + " を確定しました。総務部長承認をお願いします。"
    outbox.defer(op.db, "notifyGeneralAffairs_", [snapshot, ga_subject, {"heading": ga_heading}, None], guard=request)
    outbox.defer(op.db, "sendApprovalRequestEmail_", [snapshot, gm, over_banner if over else ""], guard=request)
    if selection:
        outbox.defer(op.db, "regeneratePdfAfterSplit_", [snapshot, STEPS.GENERAL_MANAGER, op.user], guard=request)
        outbox.defer(op.db, "notifySplitReturned_",
                     [split_request_id, STEPS.PURCHASING_QUOTE, clean_comment, op.name], request_id=split_request_id)
    return _detail(op, request.request_id)


EXPEDITE_PREFIX = "【⚡至急承認・金額確定不要⚡】"


def confirm_expedited(ctx: RpcContext, requestId: Any = None, comment: Any = "") -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_approve(op, request) or request.current_step != STEPS.PURCHASING_QUOTE:
        raise RpcError("購買担当者のみ操作できます。")
    rule = op.masters.require_approver_rule(request.applicant_email, request.department)
    gm = op.masters.get_approver_for_step(rule, STEPS.GENERAL_MANAGER)
    clean_comment = sanitize_text(comment, 1000)
    now = store.now()

    store.touch(request, now)
    request.amount_waived = True
    request.route_json = _route_json(STANDARD_ROUTE)
    request.status = STATUS.IN_REVIEW
    request.current_step = STEPS.GENERAL_MANAGER
    request.current_approver_email = gm["email"]
    request.current_approver_name = gm["name"]
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.EXPEDITE, from_status=STATUS.IN_REVIEW, to_status=STATUS.IN_REVIEW,
        from_step=STEPS.PURCHASING_QUOTE, to_step=STEPS.GENERAL_MANAGER,
        comment=" ".join(p for p in ("金額確定不要・至急手配を依頼", clean_comment) if p),
    )
    snapshot = _snapshot(request)
    banner = "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "⚡⚡⚡ 至急承認のお願い（金額確定不要）⚡⚡⚡",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "この申請は金額確定が不要な標準品（価格は商社と確定済み）です。",
        "生産を止めないため、購買にて至急手配を開始したい案件です。",
        "金額確定は行いません。総務部長の至急承認をお願いします。",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ])
    heading = banner + "\n\n購買より、金額確定不要・至急手配の申請です。" + (("\n備考: " + clean_comment) if clean_comment else "")
    outbox.defer(op.db, "notifyGeneralAffairs_",
                 [snapshot, EXPEDITE_PREFIX + "[貯蔵品購入申請] " + request.request_id, {"heading": heading}, None],
                 guard=request)
    outbox.defer(op.db, "sendApprovalRequestEmail_", [snapshot, gm, banner, EXPEDITE_PREFIX], guard=request)
    return _detail(op, request.request_id)


def arrange_complete(ctx: RpcContext, requestId: Any = None, comment: Any = "") -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_approve(op, request) or request.current_step != STEPS.PURCHASING:
        raise RpcError("購買担当者のみ手配完了できます。")
    if not (parse_number(request.total_amount) > 0) and not parse_boolean(bool(request.amount_waived)):
        raise RpcError("確定金額が未入力です。見積（金額入力）が未完了の案件は、差戻し→再申請してください。")
    now = store.now()
    _apply_transition(request, STEPS.DONE, {"email": "", "name": ""}, now)
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.COMPLETE, from_status=STATUS.IN_REVIEW, to_status=STATUS.COMPLETED,
        from_step=STEPS.PURCHASING, to_step=STEPS.DONE, comment=sanitize_text(comment, 1000),
    )
    outbox.defer(op.db, "completeAndNotify_", [_snapshot(request), op.user], guard=request)
    return _detail(op, request.request_id)


def _trash_pdf_later(op: Op, request: Request) -> None:
    """取消・却下: ドライブの PDF をゴミ箱へ（GAS 版はその場で消していた。VPS 版は後回しの仕事）。"""
    if request.pdf_file_id:
        outbox.defer(op.db, "trashPdf", [request.pdf_file_id, request.request_id], request_id=request.request_id)


def return_request(
    ctx: RpcContext, requestId: Any = None, comment: Any = "", kiosk: Any = False, discard: Any = False,  # noqa: N803
    expectedStep: Any = None,  # noqa: N803
) -> dict[str, Any]:
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    assert_expected_step(request, expectedStep)
    is_kiosk = js_truthy(kiosk)
    is_discard = js_truthy(discard)
    if is_kiosk:
        assert_kiosk_target(request)
        if is_discard:
            raise RpcError("全画面モードでは却下できません。")
    elif not can_approve(op, request):
        raise RpcError("現在の承認者のみ差戻し・却下できます。")

    clean_comment = sanitize_text(comment, 1000)
    if not clean_comment:
        raise RpcError("却下理由（コメント）を入力してください。" if is_discard else "差戻しコメントを入力してください。")

    act_email, act_name, hist_comment = op.email, op.name, clean_comment
    if is_kiosk:
        ka = kiosk_attribution(request, op, comment)
        act_email, act_name, hist_comment = ka["email"], ka["name"], ka["comment"]

    before = _snapshot(request)
    from_status, from_step = request.status, request.current_step
    now = store.now()
    if is_discard:
        _trash_pdf_later(op, request)
        store.touch(request, now)
        request.status = STATUS.CANCELLED
        request.current_approver_email = ""
        request.current_approver_name = ""
        request.pdf_file_id = ""
        request.pdf_url = ""
        store.add_history(
            op.db, request_id=request.request_id, at=now, actor_email=act_email, actor_name=act_name,
            action=ACTION.REJECT, from_status=from_status, to_status=STATUS.CANCELLED,
            from_step=from_step, to_step=from_step, comment=hist_comment,
        )
        outbox.defer(op.db, "sendRejectedEmail_", [_snapshot(request), clean_comment], guard=request)
        return _detail(op, request.request_id)

    store.touch(request, now)
    request.status = STATUS.RETURNED
    request.current_step = STEPS.APPLICANT
    request.current_approver_email = ""
    request.current_approver_name = ""
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=act_email, actor_name=act_name,
        action=ACTION.RETURN, from_status=from_status, to_status=STATUS.RETURNED,
        from_step=from_step, to_step=STEPS.APPLICANT, comment=hist_comment,
    )
    outbox.defer(op.db, "sendReturnedEmail_", [_snapshot(request), clean_comment], guard=request)
    # 出来事の共有なので状態照合なし。差戻し段階は申請者へ戻す前の段。
    outbox.defer(op.db, "sendReturnedBroadcast_", [before, from_step, clean_comment, act_name],
                 request_id=request.request_id)
    return _detail(op, request.request_id)


# ===== 1 段リコール =====


def resolve_step_approver_for_recall(op: Op, request: Request, step: str, rule: dict[str, Any]) -> dict[str, Any]:
    if step == STEPS.SUPERVISOR:
        supervisors = op.masters.require_supervisors(request.applicant_email, request.department)
        return {"approver": {"email": supervisors[0]["email"], "name": supervisors[0]["name"]}, "members": supervisors}
    approver = op.masters.get_approver_for_step(rule, step, request.category)
    members = op.masters.resolve_step_approvers_with_star(rule, step, request.category)
    return {"approver": approver, "members": members if members else [approver]}


def recall_step(ctx: RpcContext, requestId: Any = None, comment: Any = "") -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    route = json_parse(request.route_json, [])
    if not can_recall(op, request):
        raise RpcError("この申請を前段へ戻す権限がありません。")
    current_index = route.index(request.current_step) if request.current_step in route else -1
    if current_index <= 0:
        raise RpcError("先頭ステップのため前段へ戻せません。申請者へ戻す場合は「差戻し」をご利用ください。")
    clean_comment = sanitize_text(comment, 1000)
    if not clean_comment:
        raise RpcError("戻す理由（コメント）を入力してください。")

    prev_step = route[current_index - 1]
    now = store.now()
    rule = op.masters.require_approver_rule(request.applicant_email, request.department)
    from_status, from_step = request.status, request.current_step

    store.touch(request, now)
    request.status = STATUS.IN_REVIEW
    if prev_step == STEPS.PURCHASING_QUOTE:
        # 購買（見積）へ戻すときは金額不要を外し、経路を標準に戻す。合計はわざと残す。
        request.amount_waived = False
        request.route_json = _route_json(STANDARD_ROUTE)
    resolved = resolve_step_approver_for_recall(op, request, prev_step, rule)
    request.current_step = prev_step
    request.current_approver_email = resolved["approver"]["email"]
    request.current_approver_name = resolved["approver"]["name"]
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.RECALL, from_status=from_status, to_status=STATUS.IN_REVIEW,
        from_step=from_step, to_step=prev_step, comment=clean_comment,
    )
    snapshot = _snapshot(request)
    for member in resolved["members"]:
        outbox.defer(op.db, "sendApprovalRequestEmail_", [snapshot, member], guard=request)
    return _detail(op, request.request_id)


# ===== 取消 =====


def delete_request(ctx: RpcContext, requestId: Any = None, comment: Any = "") -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_delete(op, request):
        raise RpcError("この申請を取り消す権限がありません。")
    _trash_pdf_later(op, request)
    from_status, from_step = request.status, request.current_step
    now = store.now()
    store.touch(request, now)
    request.status = STATUS.CANCELLED
    request.current_approver_email = ""
    request.current_approver_name = ""
    request.pdf_file_id = ""
    request.pdf_url = ""
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=op.email, actor_name=op.name,
        action=ACTION.CANCEL, from_status=from_status, to_status=STATUS.CANCELLED,
        from_step=from_step, to_step=from_step, comment=sanitize_text(comment, 1000),
    )
    return _detail(op, request.request_id)


# ===== PDF の再作成（手動） =====


def generate_request_pdf(ctx: RpcContext, requestId: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    if not can_generate_pdf(op, request):
        raise RpcError("完了済み申請のみPDFを作成できます。")
    # VPS 版: その場で作らず後回しの仕事に積む（画面は pendingJobs が 0 になるまで詳細を取り直す）。
    outbox.defer(op.db, "regeneratePdf", [request.request_id, op.user], guard=request)
    return _detail(op, request.request_id)


# ===== 社長決裁 =====


def record_president_decision(
    ctx: RpcContext, requestId: Any = None, decision: Any = None, comment: Any = "", kiosk: Any = False,  # noqa: N803
    returnItems: Any = None, expectedStep: Any = None,  # noqa: N803
) -> dict[str, Any]:
    op = Op(ctx)
    request = store.require_request(op.db, requestId, lock=True)
    assert_expected_step(request, expectedStep)
    is_kiosk = js_truthy(kiosk)
    if is_kiosk and not is_president_pending_row(request):
        raise RpcError("社長決裁待ちの申請ではありません。画面を開き直してください。")
    if not is_kiosk and not can_president(op, request):
        raise RpcError("社長または管理者（総務部長）のみ社長決裁を記録できます。")

    president_member = is_current_approver(request, op.email) or op.masters.is_step_approver_for(
        op.email, request.applicant_email, request.department, STEPS.PRESIDENT
    )
    president_name = op.name if president_member else (request.current_approver_name or STEP_LABELS[STEPS.PRESIDENT])
    president_email = op.email if president_member else request.current_approver_email
    operator_note = "" if president_member else "（" + op.name + "が社長決裁モードで記録）"
    clean_comment = sanitize_text(comment, 1000)
    hist_comment = " ".join(p for p in (clean_comment, operator_note) if p)
    now = store.now()

    if decision == "return":
        if not clean_comment:
            raise RpcError("差戻し理由を入力してください。")
        before = _snapshot(request)
        from_status = request.status
        store.touch(request, now)
        request.status = STATUS.RETURNED
        request.current_step = STEPS.APPLICANT
        request.current_approver_email = ""
        request.current_approver_name = ""
        store.add_history(
            op.db, request_id=request.request_id, at=now, actor_email=president_email, actor_name=president_name,
            action=ACTION.RETURN, from_status=from_status, to_status=STATUS.RETURNED,
            from_step=STEPS.PRESIDENT, to_step=STEPS.APPLICANT, comment=hist_comment,
        )
        outbox.defer(op.db, "sendReturnedEmail_", [_snapshot(request), clean_comment], guard=request)
        outbox.defer(op.db, "sendReturnedBroadcast_", [before, STEPS.PRESIDENT, clean_comment, president_name],
                     request_id=request.request_id)
        return _detail(op, request.request_id)

    if decision != "approve":
        raise RpcError("不正な操作です。")

    route = json_parse(request.route_json, [])
    if STEPS.PRESIDENT not in route:
        raise RpcError("承認経路が不正です。管理者に確認してください。")
    index = route.index(STEPS.PRESIDENT)
    next_step = route[index + 1] if index + 1 < len(route) else STEPS.DONE
    next_approver: dict[str, str] = {"email": "", "name": ""}
    if next_step != STEPS.DONE:
        rule = op.masters.require_approver_rule(request.applicant_email, request.department)
        next_approver = op.masters.get_approver_for_step(rule, next_step, request.category)

    split_happened = False
    if isinstance(returnItems, list) and len(returnItems) > 0:
        split_items_on_approval(op, request, returnItems, clean_comment, kiosk, now)
        split_happened = True

    from_status = request.status
    _apply_transition(request, next_step, next_approver, now)
    store.add_history(
        op.db, request_id=request.request_id, at=now, actor_email=president_email, actor_name=president_name,
        action=ACTION.COMPLETE if next_step == STEPS.DONE else ACTION.APPROVE,
        from_status=from_status, to_status=request.status, from_step=STEPS.PRESIDENT,
        to_step=request.current_step, comment=hist_comment,
    )
    snapshot = _snapshot(request)
    outbox.defer(op.db, "dispatchApprovalNotifications_",
                 [snapshot, STEPS.PRESIDENT, next_step, next_approver, op.user], guard=request)
    if split_happened:
        outbox.defer(op.db, "regeneratePdfAfterSplit_", [snapshot, next_step, op.user], guard=request)
    return _detail(op, request.request_id)
