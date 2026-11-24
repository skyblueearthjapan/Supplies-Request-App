"""表の読み書き（Storage.gs の代わり）。

- 書き込みの操作は申請の行を with_for_update() で取る（GAS 版の全体ロックの代わり）。
- 明細の ID は変えない（replace_items は同じ位置・同じ ID の行を書き換える）。
- 時刻は呼び出し側が 1 回の操作で 1 つの now を使う（GAS 版の nowString_ は同じ秒）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.helpers import js_string, js_truthy, parse_number, sanitize_text, split_emails, to_decimal
from app.domain.labels import DEFAULT_SETTINGS, DEFAULT_THRESHOLD
from app.models import History, Request, RequestItem, Setting
from app.rpc import RpcError
from app.serialize import item_row
from app.timeutil import new_id, now_utc


def now() -> datetime:
    return now_utc()


def unique_id(db: Session, model: type, prefix: str, taken: set[str] | None = None) -> str:
    """createId_ と同じ形の ID。同じ秒に作った ID と重ならないよう、使われていない値を選ぶ。"""
    taken = taken if taken is not None else set()
    for _ in range(50):
        candidate = new_id(prefix)
        if candidate in taken:
            continue
        if model is History:
            exists = db.execute(select(History.seq).where(History.history_id == candidate)).first() is not None
        else:
            exists = db.get(model, candidate) is not None
        if not exists:
            taken.add(candidate)
            return candidate
    raise RuntimeError("ID を作れませんでした")  # pragma: no cover


# ===== 申請 =====


def get_request(db: Session, request_id: Any, lock: bool = False) -> Request | None:
    rid = js_string(request_id)
    if not rid:
        return None
    stmt = select(Request).where(Request.request_id == rid)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def require_request(db: Session, request_id: Any, lock: bool = False) -> Request:
    request = get_request(db, request_id, lock=lock)
    if request is None:
        raise RpcError("申請が見つかりません。")
    return request


def touch(request: Request, at: datetime) -> None:
    """updatedAt を進め、更新回数（row_version）を 1 つ増やす。"""
    request.updated_at = at
    request.row_version = (request.row_version or 0) + 1


# ===== 明細 =====


def get_items(db: Session, request_id: str) -> list[RequestItem]:
    return list(
        db.execute(
            select(RequestItem).where(RequestItem.request_id == request_id).order_by(RequestItem.line_no, RequestItem.item_id)
        ).scalars()
    )


def get_item_rows(db: Session, request_id: str) -> list[dict[str, Any]]:
    return [item_row(i) for i in get_items(db, request_id)]


def _apply_item(row: RequestItem, item: dict[str, Any], line_no: int) -> None:
    from app.domain.helpers import parse_date_input

    row.line_no = line_no
    row.name = js_string(item.get("name"))
    row.model = js_string(item.get("model"))
    row.maker = js_string(item.get("maker"))
    row.quantity = to_decimal(item.get("quantity"))
    row.unit_price = to_decimal(item.get("unitPrice"))
    row.amount = to_decimal(item.get("amount"))
    ddd = item.get("desiredDeliveryDate")
    row.desired_delivery_date = ddd if not isinstance(ddd, str) else parse_date_input(ddd)
    row.note = js_string(item.get("note"))
    row.unit = js_string(item.get("unit"))


def replace_items(db: Session, request_id: str, items: list[dict[str, Any]]) -> list[RequestItem]:
    """replaceItems_ の代わり。行番号は 1 から振り直す。明細 ID は変えない:

    - 渡した明細に、この申請の itemId が 1 つでもあれば ID で対応させる（金額確定・分割）
    - なければ位置で対応させる（申請の修正。画面は itemId を送らない）
    余った古い行は消し、足りない行は新しい ID で作る。
    """
    existing = get_items(db, request_id)
    by_id = {row.item_id: row for row in existing}
    explicit = any(js_string(item.get("itemId")) in by_id for item in items)
    used: set[str] = set()
    taken: set[str] = set()
    result: list[RequestItem] = []
    for index, item in enumerate(items):
        row: RequestItem | None = None
        if explicit:
            item_id = js_string(item.get("itemId"))
            if item_id in by_id and item_id not in used:
                row = by_id[item_id]
        elif index < len(existing):
            row = existing[index]
        if row is None:
            row = RequestItem(item_id=unique_id(db, RequestItem, "ITEM", taken), request_id=request_id)
            db.add(row)
        used.add(row.item_id)
        _apply_item(row, item, index + 1)
        result.append(row)
    for row in existing:
        if row.item_id not in used:
            db.delete(row)
    db.flush()
    return result


def move_items(db: Session, items: list[RequestItem], to_request_id: str) -> None:
    """分割: 明細を別の申請へ移す（ID はそのまま。単価・金額は 0 にする）。行番号は移った順に振る。"""
    for index, row in enumerate(items):
        row.request_id = to_request_id
        row.line_no = index + 1
        row.unit_price = to_decimal(0)
        row.amount = to_decimal(0)
    db.flush()


# ===== 履歴 =====


def get_history(db: Session, request_id: str) -> list[History]:
    return list(
        db.execute(
            select(History).where(History.request_id == request_id).order_by(History.happened_at, History.seq)
        ).scalars()
    )


def add_history(
    db: Session,
    *,
    request_id: str,
    at: datetime,
    actor_email: str = "",
    actor_name: str = "",
    action: str,
    from_status: str = "",
    to_status: str = "",
    from_step: str = "",
    to_step: str = "",
    comment: str = "",
) -> History:
    row = History(
        history_id=unique_id(db, History, "HIS"),
        request_id=request_id,
        happened_at=at,
        actor_email=actor_email or "",
        actor_name=actor_name or "",
        action=action,
        from_status=from_status or "",
        to_status=to_status or "",
        from_step=from_step or "",
        to_step=to_step or "",
        comment=comment or "",
    )
    db.add(row)
    db.flush()
    return row


# ===== 設定 =====


def load_settings(db: Session) -> dict[str, str]:
    """getSettings_: {key: value}。行が無いキーは既定値で補う（GAS 版は自己修復で既定値の行を足す）。"""
    values = {row.key: row.value for row in db.execute(select(Setting)).scalars() if row.key}
    for setting in DEFAULT_SETTINGS:
        values.setdefault(setting["key"], setting["value"])
    return values


def threshold_amount(settings: dict[str, str]) -> float:
    threshold = parse_number(settings.get("thresholdAmount"))
    return threshold if threshold > 0 else DEFAULT_THRESHOLD


def email_notifications_enabled(settings: dict[str, str]) -> bool:
    raw = settings.get("enableEmailNotifications")
    if raw is False:
        return False
    if raw is None or raw == "":
        return True
    return js_string(raw).strip().lower() != "false"


def save_settings(db: Session, values: dict[str, Any]) -> None:
    """saveSettings_: 決まったキーだけ、値を文字にして書く（説明は既定の文）。"""
    rows = {row.key: row for row in db.execute(select(Setting)).scalars()}
    for order, setting in enumerate(DEFAULT_SETTINGS, start=1):
        key = setting["key"]
        if key not in values:
            continue
        raw = values[key]
        value = js_string(raw).strip()
        row = rows.get(key)
        if row is None:
            db.add(Setting(key=key, value=value, description=setting["description"], sort_order=order))
        else:
            row.value = value
            row.description = setting["description"]
    db.flush()


def save_raw_setting(db: Session, key: str, value: str, description: str = "") -> None:
    """決まったキー以外（テスト転送など、VPS 版で足した値）を書く。"""
    row = db.get(Setting, key)
    if row is None:
        order = len(DEFAULT_SETTINGS) + 10
        db.add(Setting(key=key, value=value, description=description, sort_order=order))
    else:
        row.value = value
        if description:
            row.description = description
    db.flush()


def normalize_settings_input(raw: Any) -> dict[str, str]:
    """saveSettings（Workflow.gs）の値の整え方。"""
    data = raw if isinstance(raw, dict) else {}
    threshold_raw = data.get("thresholdAmount")
    threshold = max(1, parse_number(threshold_raw if js_truthy(threshold_raw) else DEFAULT_THRESHOLD))
    return {
        "thresholdAmount": js_string(threshold),
        "pdfFolderId": sanitize_text(data.get("pdfFolderId"), 256),
        "adminEmails": ",".join(split_emails(data.get("adminEmails") or "")),
        "enableEmailNotifications": "true" if js_truthy(data.get("enableEmailNotifications")) else "false",
    }
