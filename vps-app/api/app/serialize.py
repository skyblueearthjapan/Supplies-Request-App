"""DB の値 → GAS 版と同じ JSON の値（と、その逆）の共通変換。

GAS 版の約束:
- キーは camelCase（Constants.gs の *_COLUMNS と同じ名前）
- 日時は日本時間の 'yyyy-MM-dd HH:mm:ss'（T や時差を付けない）、日付は 'yyyy-MM-dd'
- 空は ''（null を返さない）
- 数値はシートのセルと同じく数（整数なら int）

*_row() はシートの 1 行を readObjects_ で読んだときの形（業務の加工前）を返す。
toClientRequest_ などの加工（ラベルの付与、真偽値の解釈）は domain 側で行う。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from app.timeutil import JST

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

# ===== 値ひとつずつ =====


def fmt_datetime(value: datetime | None) -> str:
    """日時 → 日本時間の 'yyyy-MM-dd HH:mm:ss'。時差のない値は UTC とみなす。空は ''。"""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST).strftime(DATETIME_FORMAT)


def fmt_date(value: date | datetime | None) -> str:
    """日付 → 'yyyy-MM-dd'。datetime が来たら日本時間の日付にする。空は ''。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(JST).strftime(DATE_FORMAT)
    return value.strftime(DATE_FORMAT)


def fmt_number(value: Decimal | int | float | None) -> int | float | str:
    """数 → JSON の数（整数なら int）。空（None）は ''（シートの空セルと同じ）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("真偽値は数として扱えません")
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def fmt_text(value: Any) -> str:
    return "" if value is None else str(value)


_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?$")


def parse_datetime_jst(value: str | datetime | date | None) -> datetime | None:
    """'yyyy-MM-dd HH:mm:ss'（日本時間）→ 時差付き datetime（UTC）。

    時刻が欠けた古い値（'yyyy-MM-dd'）は日本時間の 0 時として読む（決定事項: そのまま移す）。
    空は None。形が違えば ValueError。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=JST)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=JST).astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    m = _DT_RE.match(text)
    if not m:
        raise ValueError(f"日時の形が違います: {text!r}")
    y, mo, d, hh, mm, ss = m.groups()
    local = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0), tzinfo=JST)
    return local.astimezone(timezone.utc)


def parse_date(value: str | date | None) -> date | None:
    """'yyyy-MM-dd' → date。空は None。形が違えば ValueError。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(JST).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    m = _DT_RE.match(text)
    if not m:
        raise ValueError(f"日付の形が違います: {text!r}")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def bool_text(value: bool) -> str:
    """真偽値 → シートに書く 'true' / 'false'（書き戻しの形）。"""
    return "true" if value else "false"


# ===== 表の 1 行 → GAS 版の行オブジェクト =====


def request_row(r) -> dict[str, Any]:
    """models.Request → REQUEST_COLUMNS の行（amountWaived は真偽値、routeJson は JSON の文字列のまま）。"""
    return {
        "requestId": r.request_id,
        "createdAt": fmt_datetime(r.created_at),
        "updatedAt": fmt_datetime(r.updated_at),
        "submittedAt": fmt_datetime(r.submitted_at),
        "completedAt": fmt_datetime(r.completed_at),
        "applicantEmail": fmt_text(r.applicant_email),
        "applicantName": fmt_text(r.applicant_name),
        "department": fmt_text(r.department),
        "requestDate": fmt_date(r.request_date),
        "reasonCode": fmt_text(r.reason_code),
        "reasonDetail": fmt_text(r.reason_detail),
        "totalAmount": fmt_number(r.total_amount),
        "status": fmt_text(r.status),
        "currentStep": fmt_text(r.current_step),
        "currentApproverEmail": fmt_text(r.current_approver_email),
        "currentApproverName": fmt_text(r.current_approver_name),
        "routeJson": fmt_text(r.route_json),
        "pdfFileId": fmt_text(r.pdf_file_id),
        "pdfUrl": fmt_text(r.pdf_url),
        "version": fmt_text(r.version),
        "amountWaived": bool(r.amount_waived),
        "category": fmt_text(r.category),
        "splitFromRequestId": fmt_text(r.split_from_request_id),
    }


def item_row(i) -> dict[str, Any]:
    """models.RequestItem → ITEM_COLUMNS の行。"""
    return {
        "itemId": i.item_id,
        "requestId": i.request_id,
        "lineNo": i.line_no,
        "name": fmt_text(i.name),
        "model": fmt_text(i.model),
        "maker": fmt_text(i.maker),
        "quantity": fmt_number(i.quantity),
        "unitPrice": fmt_number(i.unit_price),
        "amount": fmt_number(i.amount),
        "desiredDeliveryDate": fmt_date(i.desired_delivery_date),
        "note": fmt_text(i.note),
        "unit": fmt_text(i.unit),
    }


def history_row(h) -> dict[str, Any]:
    """models.History → HISTORY_COLUMNS の行（並べるときは happened_at, seq の順）。"""
    return {
        "historyId": h.history_id,
        "requestId": h.request_id,
        "happenedAt": fmt_datetime(h.happened_at),
        "actorEmail": fmt_text(h.actor_email),
        "actorName": fmt_text(h.actor_name),
        "action": fmt_text(h.action),
        "fromStatus": fmt_text(h.from_status),
        "toStatus": fmt_text(h.to_status),
        "fromStep": fmt_text(h.from_step),
        "toStep": fmt_text(h.to_step),
        "comment": fmt_text(h.comment),
    }


APPROVER_FIELDS = (
    ("department", "department"),
    ("applicantEmail", "applicant_email"),
    ("applicantName", "applicant_name"),
    ("supervisorEmail", "supervisor_email"),
    ("supervisorName", "supervisor_name"),
    ("generalManagerEmail", "general_manager_email"),
    ("generalManagerName", "general_manager_name"),
    ("presidentEmail", "president_email"),
    ("presidentName", "president_name"),
    ("purchasingEmail", "purchasing_email"),
    ("purchasingName", "purchasing_name"),
    ("active", "active"),
    ("supervisorTitle", "supervisor_title"),
    ("generalManagerTitle", "general_manager_title"),
    ("presidentTitle", "president_title"),
    ("purchasingTitle", "purchasing_title"),
    ("purchasingElecEmail", "purchasing_elec_email"),
    ("purchasingElecName", "purchasing_elec_name"),
    ("purchasingElecTitle", "purchasing_elec_title"),
)


def approver_row(a) -> dict[str, Any]:
    """models.ApproverRule → APPROVER_COLUMNS の行（active は真偽値）。"""
    return {key: (bool(a.active) if key == "active" else fmt_text(getattr(a, attr))) for key, attr in APPROVER_FIELDS}


def worker_row(w) -> dict[str, Any]:
    """models.Worker → WORKER_CACHE_COLUMNS の行。画面向けの {code, name, ...} への変換は domain 側。"""
    return {
        "workerCode": fmt_text(w.worker_code),
        "name": fmt_text(w.name),
        "dept": fmt_text(w.dept),
        "location": fmt_text(w.location),
        "staffType": fmt_text(w.staff_type),
    }


def recipient_row(r) -> dict[str, Any]:
    """models.NotificationRecipient → RECIPIENT_COLUMNS の行（getRecipients_ の形）。"""
    return {
        "type": fmt_text(r.type),
        "email": fmt_text(r.email),
        "name": fmt_text(r.name),
        "active": bool(r.active),
        "sendAs": fmt_text(r.send_as),
    }


def dept_supervisor_row(d) -> dict[str, Any]:
    """models.DeptSupervisor → DEPT_SUPERVISOR_COLUMNS の行。"""
    return {
        "department": fmt_text(d.department),
        "email": fmt_text(d.email),
        "name": fmt_text(d.name),
        "active": bool(d.active),
        "title": fmt_text(d.title),
        "notifyEmail": fmt_text(d.notify_email),
    }


def settings_map(rows) -> dict[str, str]:
    """models.Setting の並び → {key: value}（getSettings_ と同じ。値は文字列）。"""
    return {s.key: fmt_text(s.value) for s in rows if s.key}
