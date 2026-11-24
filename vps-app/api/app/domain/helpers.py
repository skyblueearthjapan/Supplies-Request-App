"""Helpers.gs の移植。JS と同じ結果になるように、文字・数・真偽値の読み方を合わせる。

金額の計算は JS と同じく浮動小数（float）で行う（Math.round / Math.floor と同じ結果にするため）。
DB へ入れるときに Decimal にする（to_decimal）。
"""
from __future__ import annotations

import json
import math
import re
from datetime import date
from decimal import Decimal
from typing import Any

from app.constants import CATEGORIES, STATUS, STEPS
from app.domain.labels import CATEGORY_LABELS, REASONS, STATUS_LABELS, STEP_LABELS, TAX_RATE, UNIT_OTHER
from app.serialize import parse_date, request_row


def js_string(value: Any) -> str:
    """JS の String(value)。None は ''（GAS 版は null/undefined を '' に寄せている）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return js_string(float(value))
    return str(value)


def js_truthy(value: Any) -> bool:
    """JS の真偽の判定（Boolean(value)）。"""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (value == 0 or (isinstance(value, float) and math.isnan(value)))
    if isinstance(value, str):
        return value != ""
    return True


def sanitize_text(value: Any, max_length: int = 0) -> str:
    """sanitizeText_: String(value).trim() を max_length で黙って切る。"""
    text = js_string(value).strip()
    if max_length and len(text) > max_length:
        return text[:max_length]
    return text


def normalize_email(value: Any) -> str:
    return sanitize_text(value, 256).lower()


def strip_role_paren(name: Any) -> str:
    """氏名欄の役職カッコ（例「五十嵐（購買）」）を除いて姓名だけにする。"""
    return re.sub(r"[（(][^）)]*[）)]", "", js_string(name)).strip()


def parse_number(value: Any) -> float:
    """parseNumber_: '' / None は 0。カンマを除いて Number()。有限でなければ 0。"""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0  # Number('true') は NaN
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, Decimal):
        number = float(value)
    else:
        text = str(value).replace(",", "").strip()
        if text == "":
            return 0
        try:
            if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text):
                number = float(text)
            elif re.fullmatch(r"0[xX][0-9a-fA-F]+", text):
                number = float(int(text, 16))
            else:
                return 0
        except ValueError:
            return 0
    if math.isnan(number) or math.isinf(number):
        return 0
    return int(number) if number.is_integer() else number


def parse_boolean(value: Any) -> bool:
    """parseBoolean_: 'true'（大小無視）か true だけが真。"""
    return value is True or js_string(value).lower() == "true"


def split_emails(value: Any) -> list[str]:
    return [e for e in (normalize_email(p) for p in re.split(r"[,\n;]", js_string(value) if value else "")) if e]


def normalize_email_list(value: Any) -> str:
    seen: list[str] = []
    for email in split_emails(value):
        if email not in seen:
            seen.append(email)
    return ", ".join(seen)


def js_round(value: float) -> int:
    """Math.round（.5 は大きい方へ）。"""
    return int(math.floor(value + 0.5))


def tax_amount(net: Any) -> int:
    """消費税額。端数は切り捨て。税抜小計にまとめて 1 回だけかける。"""
    return int(math.floor(parse_number(net) * TAX_RATE))


def gross_amount(net: Any) -> float:
    n = parse_number(net)
    return n + tax_amount(n)


def format_currency(value: Any) -> str:
    """formatCurrency_: '¥' + 3 桁区切り（四捨五入して整数）。"""
    return "¥" + f"{js_round(parse_number(value)):,}"


def json_parse(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def to_decimal(value: Any) -> Decimal:
    number = parse_number(value)
    return Decimal(js_string(number))


def get_reason_label(code: str) -> str:
    for reason in REASONS:
        if reason["code"] == code:
            return reason["label"]
    return ""


def normalize_category(category: Any) -> str:
    """未知・空（旧申請）は 一般不明 に倒す。旧英語コードも読み替える。"""
    value = sanitize_text(category, 20)
    if value in (CATEGORIES.MECH, CATEGORIES.ELEC, CATEGORIES.GENERAL):
        return value
    legacy = {"MECH": CATEGORIES.MECH, "ELEC": CATEGORIES.ELEC, "GENERAL": CATEGORIES.GENERAL}
    return legacy.get(value.upper(), CATEGORIES.GENERAL)


def normalize_unit(value: Any) -> str:
    unit = sanitize_text(value, 20)
    return "" if unit == UNIT_OTHER else unit


def parse_date_input(text: str) -> date | None:
    """画面から来た日付（yyyy-MM-dd）を date にする。空は None、形が違えば ValueError。"""
    if not text:
        return None
    return parse_date(text)


def to_client_request(r) -> dict[str, Any]:
    """toClientRequest_。r は models.Request か、serialize.request_row の dict。"""
    row = r if isinstance(r, dict) else request_row(r)
    category = normalize_category(row["category"])
    return {
        "requestId": row["requestId"],
        "createdAt": row["createdAt"],
        "updatedAt": row["updatedAt"],
        "submittedAt": row["submittedAt"],
        "completedAt": row["completedAt"],
        "applicantEmail": row["applicantEmail"],
        "applicantName": row["applicantName"],
        "department": row["department"],
        "requestDate": row["requestDate"],
        "reasonCode": row["reasonCode"],
        "reasonLabel": get_reason_label(row["reasonCode"]),
        "reasonDetail": row["reasonDetail"],
        "category": category,
        "categoryLabel": CATEGORY_LABELS[category],
        "totalAmount": parse_number(row["totalAmount"]),
        "status": row["status"],
        "statusLabel": STATUS_LABELS.get(row["status"], row["status"]),
        "currentStep": row["currentStep"],
        "currentStepLabel": STEP_LABELS.get(row["currentStep"], row["currentStep"]),
        "currentApproverEmail": row["currentApproverEmail"],
        "currentApproverName": row["currentApproverName"],
        "route": json_parse(row["routeJson"], []),
        "isPresidentPending": row["status"] == STATUS.IN_REVIEW and row["currentStep"] == STEPS.PRESIDENT,
        "amountWaived": parse_boolean(row["amountWaived"]),
        "pdfFileId": row["pdfFileId"],
        "pdfUrl": row["pdfUrl"],
        "splitFromRequestId": row["splitFromRequestId"] or "",
        "version": row["version"],
    }
