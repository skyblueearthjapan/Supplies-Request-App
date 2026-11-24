"""メール・PDF 担当（M）の共通部品。

GAS 版 Constants.gs / Helpers.gs のうち、宛先・本文・PDF が使う値と小さな関数だけを移植したもの。
業務ロジック側（domain/）にも同じものがあるはずなので、まとめるときはどちらか一方に寄せてよい。
DB や設定には依存しない（引数で受け取る）。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from urllib.parse import quote

JST = timezone(timedelta(hours=9), "Asia/Tokyo")

APP_NAME = "貯蔵品購入申請"
DEFAULT_THRESHOLD = 100000
TAX_RATE = 0.10


class STEPS:
    SUPERVISOR = "上席"
    PURCHASING_QUOTE = "購買見積"
    GENERAL_MANAGER = "総務部長"
    PRESIDENT = "社長"
    PURCHASING = "購買手配"
    APPLICANT = "申請者"
    DONE = "完了済"


STEP_LABELS = {
    "上席": "上席",
    "購買見積": "購買（見積）",
    "総務部長": "総務部長",
    "社長": "社長",
    "購買手配": "購買",
    "申請者": "申請者",
    "完了済": "完了",
}


class STATUS:
    IN_REVIEW = "承認中"
    RETURNED = "差戻し"
    COMPLETED = "完了"
    CANCELLED = "取消"


STATUS_LABELS = {"承認中": "承認中", "差戻し": "差戻し", "完了": "完了", "取消": "取消"}


class ACTION:
    SUBMIT = "申請"
    APPROVE = "承認"
    RETURN = "差戻"
    RESUBMIT = "再申請"
    UPDATE = "更新"
    COMPLETE = "完了処理"
    ESCALATE = "社長決裁へ"
    QUOTE = "金額確定"
    EXPEDITE = "金額不要至急"
    PDF_GENERATE = "PDF作成"
    CANCEL = "取消処理"
    RECALL = "前段戻し"
    REJECT = "却下"
    SPLIT_OUT = "明細分離"


ACTION_LABELS = {
    "申請": "申請",
    "承認": "承認",
    "差戻": "差戻し",
    "再申請": "再申請",
    "更新": "更新",
    "完了処理": "完了",
    "社長決裁へ": "社長決裁へ",
    "金額確定": "金額確定",
    "金額不要至急": "金額不要・至急",
    "PDF作成": "PDF作成",
    "取消処理": "取消",
    "前段戻し": "前段へ戻す",
    "却下": "却下",
    "明細分離": "明細を分離",
}


class RECIPIENT_TYPES:
    GENERAL_AFFAIRS = "総務部"
    PURCHASING = "購買"
    PURCHASING_ELEC = "電気購買"


class CATEGORIES:
    MECH = "メカ"
    ELEC = "電気"
    GENERAL = "一般不明"


CATEGORY_LABELS = {"メカ": "メカ", "電気": "電気", "一般不明": "一般・不明"}

REASONS = [
    {"code": "A", "label": "4月以降も都度購入を認めた消耗品（ウエス、オイル等）"},
    {"code": "B", "label": "対象工番が特殊案件で新規に購入しなければ対応できないもの"},
    {"code": "C", "label": "年度末に購入した消耗品を使い切った場合"},
    {"code": "D", "label": "工具等を修理する場合"},
    {"code": "E", "label": "工具等を修理するよりも新規で購入したほうが安価な場合"},
    {"code": "F", "label": "社長が特別に認可した場合"},
]


# ---------------------------------------------------------------------------
# Helpers.gs の移植
# ---------------------------------------------------------------------------

def js_str(value: Any) -> str:
    """JavaScript の String(value) に近い文字列化（数値の 3.0 は '3'、None は ''）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime):
        return format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def sanitize_text(value: Any, max_length: int | None = None) -> str:
    text = js_str(value).strip()
    if max_length and len(text) > max_length:
        return text[:max_length]
    return text


def normalize_email(value: Any) -> str:
    return sanitize_text(value, 256).lower()


def strip_role_paren(name: Any) -> str:
    """氏名欄の役職カッコ（例「五十嵐（購買）」）を除く。"""
    return re.sub(r"[（(][^）)]*[）)]", "", js_str(name)).strip()


def parse_number(value: Any) -> float:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else 0
    text = str(value).replace(",", "").strip()
    if text == "":
        return 0  # JS: Number('') === 0
    try:
        number = float(text)
    except ValueError:
        return 0
    return number if math.isfinite(number) else 0


def parse_boolean(value: Any) -> bool:
    return value is True or js_str(value).lower() == "true"


def parse_active(value: Any) -> bool:
    """有効の欄: 空なら有効（GAS: row.active === '' ? true : parseBoolean_）。DB の NULL も空とみなす。"""
    if value is None or value == "":
        return True
    return parse_boolean(value)


def split_emails(value: Any) -> list[str]:
    return [e for e in (normalize_email(p) for p in re.split(r"[,\n;]", js_str(value))) if e]


def normalize_email_list(value: Any) -> str:
    seen: set[str] = set()
    out = []
    for email in split_emails(value):
        if email not in seen:
            seen.add(email)
            out.append(email)
    return ", ".join(out)


def js_round(value: float) -> int:
    """Math.round（.5 は正の方向へ）。"""
    return math.floor(value + 0.5)


def format_currency(value: Any) -> str:
    return "¥" + f"{js_round(parse_number(value)):,}"


def tax_amount(net: Any) -> int:
    """消費税（切り捨て）。税抜の合計に 1 回だけかける。"""
    return math.floor(parse_number(net) * TAX_RATE)


def gross_amount(net: Any) -> float:
    n = parse_number(net)
    return n + tax_amount(n)


def qty_text(quantity: Any, unit: Any) -> str:
    text = js_str(quantity)
    suffix = sanitize_text(unit, 20)
    return text + " " + suffix if suffix else text


def sanitize_file_name(value: Any) -> str:
    return re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', "_", sanitize_text(value, 120)) or "申請者"


def normalize_category(category: Any) -> str:
    value = sanitize_text(category, 20)
    if value in (CATEGORIES.MECH, CATEGORIES.ELEC, CATEGORIES.GENERAL):
        return value
    legacy = {"MECH": CATEGORIES.MECH, "ELEC": CATEGORIES.ELEC, "GENERAL": CATEGORIES.GENERAL}
    return legacy.get(value.upper(), CATEGORIES.GENERAL)


def category_label(request: Mapping[str, Any]) -> str:
    return CATEGORY_LABELS[normalize_category(request.get("category"))]


def to_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        # DB から naive で来たときは UTC とみなす（timestamptz を UTC で読む前提）。
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST)


def format_datetime(value: Any) -> str:
    """日時を GAS 版と同じ 'yyyy-MM-dd HH:mm:ss'（日本時間）にする。文字列はそのまま。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return to_jst(value).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def date_only(value: Any) -> str:
    """日付部分（yyyy-MM-dd）。datetime は日本時間に直してから日付にする。"""
    if isinstance(value, datetime):
        return to_jst(value).strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return js_str(value)[:10]


def today_jst(now: datetime | None = None) -> str:
    return to_jst(now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def request_url(public_origin: str, request_id: str) -> str:
    """詳細画面の URL（GAS: getRequestUrl_ の baseUrl + '?requestId=' + encodeURIComponent）。"""
    base = (public_origin or "").rstrip("/")
    return base + "/?requestId=" + quote(js_str(request_id), safe="-_.!~*'()")


def route_of(request: Mapping[str, Any]) -> list[str]:
    route = request.get("route")
    if isinstance(route, list):
        return list(route)
    raw = request.get("routeJson")
    if not raw:
        return []
    try:
        import json

        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def email_notifications_enabled(settings: Mapping[str, Any]) -> bool:
    raw = settings.get("enableEmailNotifications")
    if raw is False:
        return False
    if raw is None or raw == "":
        return True
    return js_str(raw).strip().lower() != "false"


def threshold_amount(settings: Mapping[str, Any]) -> float:
    t = parse_number(settings.get("thresholdAmount"))
    return t if t > 0 else DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# マスタの読み替え（シート／DB の 1 行 → GAS の toClientApprover_ などと同じ形）
# ---------------------------------------------------------------------------

def to_client_approver(row: Mapping[str, Any]) -> dict[str, Any]:
    g = row.get
    return {
        "department": sanitize_text(g("department"), 100),
        "applicantEmail": normalize_email(g("applicantEmail")),
        "applicantName": sanitize_text(g("applicantName"), 100),
        "supervisorEmail": normalize_email(g("supervisorEmail")),
        "supervisorName": sanitize_text(g("supervisorName"), 100),
        "generalManagerEmail": normalize_email_list(g("generalManagerEmail")),
        "generalManagerName": sanitize_text(g("generalManagerName"), 100),
        "presidentEmail": normalize_email_list(g("presidentEmail")),
        "presidentName": sanitize_text(g("presidentName"), 100),
        "purchasingEmail": normalize_email_list(g("purchasingEmail")),
        "purchasingName": sanitize_text(g("purchasingName"), 100),
        "purchasingElecEmail": normalize_email_list(g("purchasingElecEmail")),
        "purchasingElecName": sanitize_text(g("purchasingElecName"), 100),
        "active": parse_active(g("active")),
        "supervisorTitle": sanitize_text(g("supervisorTitle"), 40),
        "generalManagerTitle": sanitize_text(g("generalManagerTitle"), 40),
        "presidentTitle": sanitize_text(g("presidentTitle"), 40),
        "purchasingTitle": sanitize_text(g("purchasingTitle"), 40),
        "purchasingElecTitle": sanitize_text(g("purchasingElecTitle"), 40),
    }


def to_dept_supervisor(row: Mapping[str, Any]) -> dict[str, Any]:
    g = row.get
    return {
        "department": sanitize_text(g("department"), 100),
        "email": normalize_email(g("email")),
        "name": sanitize_text(g("name"), 100),
        "title": sanitize_text(g("title"), 40),
        "active": parse_active(g("active")),
        "notifyEmail": normalize_email(g("notifyEmail")),
    }


def to_recipient(row: Mapping[str, Any]) -> dict[str, Any]:
    g = row.get
    return {
        "type": sanitize_text(g("type"), 50),
        "email": normalize_email(g("email")),
        "name": sanitize_text(g("name"), 100),
        "active": parse_active(g("active")),
        "sendAs": "CC" if js_str(g("sendAs")).upper() == "CC" else "TO",
    }


@dataclass
class Masters:
    """宛先・押印を決めるのに要るマスタ一式（行の順番どおり＝sort_order 順に並べて渡す）。

    行はシートの列名（camelCase）の dict。DB の行を渡すときは serialize で camelCase にしてから渡す。
    """

    approver_rules: list[dict[str, Any]] = field(default_factory=list)
    dept_supervisors: list[dict[str, Any]] = field(default_factory=list)
    recipients: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.approver_rules = [to_client_approver(r) for r in self.approver_rules]
        self.dept_supervisors = [to_dept_supervisor(r) for r in self.dept_supervisors]
        # getRecipients_ は元の email が空の行を先に捨てる。
        self.recipients = [to_recipient(r) for r in self.recipients if r.get("email")]


@dataclass
class MailPdfConfig:
    """メール・PDF・ドライブが使う環境の設定。config.py（担当 F）の値から作る。"""

    app_env: str = "staging"  # staging | production
    public_origin: str = "https://supplies.lineworks-local.net"
    mail_from: str = "imaizumi@lineworks-local.info"
    mail_from_name: str = APP_NAME
    mail_redirect_to: str = ""  # staging では必須
    staging_pdf_folder_id: str = ""  # staging の PDF 置き場（本番フォルダに書かないため）

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MailPdfConfig":
        env = environ if environ is not None else os.environ
        return cls(
            app_env=(env.get("APP_ENV") or "staging").strip().lower(),
            public_origin=env.get("PUBLIC_ORIGIN") or cls.public_origin,
            mail_from=env.get("MAIL_FROM") or cls.mail_from,
            mail_from_name=env.get("MAIL_FROM_NAME") or APP_NAME,
            mail_redirect_to=env.get("MAIL_REDIRECT_TO") or "",
            staging_pdf_folder_id=env.get("STAGING_PDF_FOLDER_ID") or "",
        )


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out
