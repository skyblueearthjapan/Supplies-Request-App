"""シートの行と DB の行（dict）の相互変換。

GAS 版（Constants.gs / Storage.gs / Helpers.gs）のシートの形を、そのまま読み書きするための決まりを持つ。
DB には依存しない。DB へのつなぎ目は「表名 → dict のリスト」で受け渡す。

読み込み（シート → DB）:
  Sheets API の UNFORMATTED_VALUE + dateTimeRenderOption=SERIAL_NUMBER の値を基本とする。
  日付のセルはシリアル値（数値）で来るため、同じ範囲の FORMATTED_VALUE（表示の文字）を
  一緒に渡すと、自由入力の列で「3/8 が日付に化けたセル」を見分けられる。
  手元の xlsx（openpyxl の datetime）や CSV（すべて文字）からの値も受け付ける。

書き出し（DB → シート）:
  valueInputOption=RAW で書く前提の値を作る。RAW はシートが値を解釈しないため、
  自由入力の「3/8」「0123」「=1+1」「1/2」がそのまま文字で残る。
  GAS 版の toCellValue_ は USER_ENTERED 相当の setValues のために先頭へ ' を付けるが、
  RAW で ' を付けると ' が値に残ってしまう。そのため RAW では ' を付けない。
  GAS 版の読み方（normalizeCellValue_）は Date 以外の値をそのまま返すので、
  RAW の文字列はそのまま GAS 版の値になる。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

# ===== Constants.gs の写し（値で照合する。キーで照合しない） =====

APP_VERSION = "1.0.0"
JST = timezone(timedelta(hours=9), "JST")
PRODUCTION_SPREADSHEET_ID = "1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4"
LEGACY_SPREADSHEET_ID = "1xCkpAIB8wPq49mzTwy6WNj3i485eV9oGwnzE-7aREQo"

STATUS_VALUES = frozenset({"承認中", "差戻し", "完了", "取消"})
STEP_VALUES = frozenset({"上席", "購買見積", "総務部長", "社長", "購買手配", "申請者", "完了済"})
ACTION_VALUES = frozenset({
    "申請", "承認", "差戻", "再申請", "更新", "完了処理", "社長決裁へ", "金額確定",
    "金額不要至急", "PDF作成", "取消処理", "前段戻し", "却下", "明細分離",
})
CATEGORY_VALUES = frozenset({"メカ", "電気", "一般不明"})
CATEGORY_LEGACY = {"MECH": "メカ", "ELEC": "電気", "GENERAL": "一般不明"}
CATEGORY_DEFAULT = "一般不明"
RECIPIENT_TYPE_VALUES = frozenset({"総務部", "購買", "電気購買"})
REASON_CODES = frozenset({"A", "B", "C", "D", "E", "F"})

# Storage.gs の FREE_TEXT_COLUMNS（GAS 版が ' を付けて書く列）。RAW で書くので ' は付けない。
FREE_TEXT_COLUMNS = frozenset({
    "applicantName", "department", "reasonDetail", "name", "model", "maker",
    "note", "unit", "actorName", "comment",
})

REQUEST_COLUMNS = [
    "requestId", "createdAt", "updatedAt", "submittedAt", "completedAt", "applicantEmail",
    "applicantName", "department", "requestDate", "reasonCode", "reasonDetail", "totalAmount",
    "status", "currentStep", "currentApproverEmail", "currentApproverName", "routeJson",
    "pdfFileId", "pdfUrl", "version", "amountWaived", "category", "splitFromRequestId",
]
REQUEST_HEADERS = [
    "申請ID", "作成日時", "更新日時", "申請日時", "完了日時",
    "申請者メール", "申請者氏名", "部署", "申請日", "理由コード",
    "理由詳細", "合計金額", "ステータス", "現在ステップ", "現在承認者メール",
    "現在承認者氏名", "承認経路(JSON)", "PDFファイルID", "PDF URL", "バージョン",
    "金額免除", "品目区分", "分離元申請ID",
]
ITEM_COLUMNS = [
    "itemId", "requestId", "lineNo", "name", "model", "maker", "quantity", "unitPrice",
    "amount", "desiredDeliveryDate", "note", "unit",
]
ITEM_HEADERS = ["明細ID", "申請ID", "行番号", "品名", "型番", "メーカー", "数量", "単価", "金額", "希望納期", "備考", "単位"]
APPROVER_COLUMNS = [
    "department", "applicantEmail", "applicantName", "supervisorEmail", "supervisorName",
    "generalManagerEmail", "generalManagerName", "presidentEmail", "presidentName",
    "purchasingEmail", "purchasingName", "active", "supervisorTitle", "generalManagerTitle",
    "presidentTitle", "purchasingTitle", "purchasingElecEmail", "purchasingElecName",
    "purchasingElecTitle",
]
APPROVER_HEADERS = [
    "部署", "申請者メール", "申請者氏名", "上席メール", "上席氏名",
    "総務部長メール", "総務部長氏名", "社長メール", "社長氏名",
    "購買メール", "購買氏名", "有効", "上席役職", "総務部長役職",
    "社長役職", "購買役職", "電気購買メール", "電気購買氏名", "電気購買役職",
]
HISTORY_COLUMNS = [
    "historyId", "requestId", "happenedAt", "actorEmail", "actorName", "action",
    "fromStatus", "toStatus", "fromStep", "toStep", "comment",
]
HISTORY_HEADERS = [
    "履歴ID", "申請ID", "発生日時", "操作者メール", "操作者氏名",
    "操作", "変更前ステータス", "変更後ステータス", "変更前ステップ", "変更後ステップ", "コメント",
]
SETTING_COLUMNS = ["key", "value", "description"]
SETTING_HEADERS = ["キー", "値", "説明"]
WORKER_CACHE_COLUMNS = ["workerCode", "name", "dept", "location", "staffType"]
WORKER_CACHE_HEADERS = ["作業員コード", "氏名", "部署", "勤務地", "区分"]
RECIPIENT_COLUMNS = ["type", "email", "name", "active", "sendAs"]
RECIPIENT_HEADERS = ["種別", "メール", "表示名", "有効", "送信区分"]
DEPT_SUPERVISOR_COLUMNS = ["department", "email", "name", "active", "title", "notifyEmail"]
DEPT_SUPERVISOR_HEADERS = ["部署", "メール", "氏名", "有効", "役職", "通知メール"]


class CodecError(ValueError):
    """シートの値が決まりに合わない（移行を止めるべき）とき。"""


@dataclass(frozen=True)
class TableSpec:
    table: str            # DB の表名
    sheet: str            # シートのタブ名
    columns: list         # 内部キー（Constants.gs の *_COLUMNS と同じ並び）
    headers: list         # 1 行目の見出し（同じ並び）
    kinds: dict           # 内部キー → 型の決まり（無ければ "text"）
    order_key: Optional[str] = None  # 行の順番を残す列（sort_order / seq）

    def kind(self, column: str) -> str:
        return self.kinds.get(column, "text")


# 型の決まり:
#   text        文字（数値は JS の String() と同じ形。123.0 → "123"）
#   date_text   文字。ただし日付のセルは日本時間の yyyy-MM-dd に直す（希望納期）
#   datetime    日時（日本時間の aware datetime）。空は None
#   date        日付（datetime.date）。空は None
#   number      Decimal。空は None
#   int         int。空は None
#   bool_false  '' は False（parseBoolean_）
#   bool_true   '' は True（有効の欄: row.active === '' ? true : parseBoolean_）
#   status / step / step_opt / status_opt / action / reason / category / recipient_type / send_as
#   route       承認経路(JSON) → list[str]
#   version     文字。書き出しは常に '1.0.0'
#   setting     設定の値（文字で持つ。真偽値は 'true'/'false'、数値は JS の String()）
TABLES: dict = {
    "requests": TableSpec(
        "requests", "申請", REQUEST_COLUMNS, REQUEST_HEADERS,
        {
            "createdAt": "datetime", "updatedAt": "datetime", "submittedAt": "datetime",
            "completedAt": "datetime", "requestDate": "date", "reasonCode": "reason",
            "totalAmount": "number", "status": "status", "currentStep": "step",
            "routeJson": "route", "version": "version", "amountWaived": "bool_false",
            "category": "category",
        },
    ),
    "request_items": TableSpec(
        "request_items", "申請明細", ITEM_COLUMNS, ITEM_HEADERS,
        {
            "lineNo": "int", "quantity": "number", "unitPrice": "number", "amount": "number",
            # 希望納期は DB の Date 型に合わせる（担当 W）。日付でない文字は移行で止める（CodecError）。
            "desiredDeliveryDate": "date",
        },
    ),
    "approver_rules": TableSpec(
        "approver_rules", "承認者マスタ", APPROVER_COLUMNS, APPROVER_HEADERS,
        {"active": "bool_true"}, order_key="sort_order",
    ),
    "history": TableSpec(
        "history", "履歴", HISTORY_COLUMNS, HISTORY_HEADERS,
        {
            "happenedAt": "datetime", "action": "action", "fromStatus": "status_opt",
            "toStatus": "status_opt", "fromStep": "step_opt", "toStep": "step_opt",
        },
        order_key="seq",
    ),
    "settings": TableSpec("settings", "設定", SETTING_COLUMNS, SETTING_HEADERS, {"value": "setting"}),
    "workers": TableSpec(
        "workers", "作業員マスタ", WORKER_CACHE_COLUMNS, WORKER_CACHE_HEADERS, {}, order_key="sort_order",
    ),
    "notification_recipients": TableSpec(
        "notification_recipients", "通知宛先マスタ", RECIPIENT_COLUMNS, RECIPIENT_HEADERS,
        {"type": "recipient_type", "active": "bool_true", "sendAs": "send_as"}, order_key="sort_order",
    ),
    "dept_supervisors": TableSpec(
        "dept_supervisors", "部署別上席マスタ", DEPT_SUPERVISOR_COLUMNS, DEPT_SUPERVISOR_HEADERS,
        {"active": "bool_true"}, order_key="sort_order",
    ),
}

# 書き出しの順番（シートのタブと同じ並び）
EXPORT_ORDER = [
    "requests", "request_items", "approver_rules", "history", "settings", "workers",
    "notification_recipients", "dept_supervisors",
]

_CAMEL = re.compile(r"(?<!^)(?=[A-Z])")


def db_key(column: str) -> str:
    """内部キー（camelCase）→ DB の列名（snake_case）。例: pdfUrl → pdf_url。"""
    return _CAMEL.sub("_", column).lower()


# ===== 値の小道具 =====

def js_string(value: Any) -> str:
    """JS の String(value) と同じ文字にする（123.0 → "123"、True → "true"、None → ""）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return str(int(value))
        return format(value.normalize(), "f")
    return str(value)


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value == "")


def serial_to_datetime(serial: float, sheet_tz: tzinfo) -> datetime:
    """シートのシリアル値（1899-12-30 起点の日数。シートのタイムゾーンの時計）→ 日本時間。"""
    seconds = round(float(serial) * 86400)
    naive = datetime(1899, 12, 30) + timedelta(seconds=seconds)
    return naive.replace(tzinfo=sheet_tz).astimezone(JST)


_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_US = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?: (\d{1,2}):(\d{2})(?::(\d{2}))?)?$")


def parse_datetime_text(text: str, sheet_tz: tzinfo) -> Optional[datetime]:
    """日時の文字を日本時間の datetime にする。読めなければ None。

    - yyyy-MM-dd[ HH:mm[:ss]]（GAS 版の nowString_ が書く形）は日本時間の時計として読む。
    - M/d/yyyy[ H:mm[:ss]]（en_US のシートで日付のセルを表示の文字で取ったとき）は
      シートのタイムゾーンの時計として読む。
    """
    s = text.strip()
    m = _ISO.match(s)
    tz = JST
    if m:
        y, mo, d, hh, mi, ss = m.groups()
    else:
        m = _US.match(s)
        if not m:
            return None
        mo, d, y, hh, mi, ss = m.groups()
        tz = sheet_tz
    try:
        naive = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    except ValueError:
        return None
    return naive.replace(tzinfo=tz).astimezone(JST)


def _looks_like_date_display(formatted: Any) -> bool:
    if not isinstance(formatted, str):
        return False
    s = formatted.strip()
    return bool(_ISO.match(s) or _US.match(s) or re.match(r"^\d{1,2}/\d{1,2}$", s))


def to_jst(value: datetime) -> datetime:
    """DB の datetime を日本時間へ。時差の無い値は日本時間の時計とみなす（SQLite で時差が落ちる場合）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def format_datetime(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return to_jst(value).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d") + " 00:00:00"
    raise CodecError(f"日時ではない値です: {value!r}")


def _to_decimal(value: Any, where: str) -> Optional[Decimal]:
    if is_blank(value):
        return None
    if isinstance(value, bool):
        raise CodecError(f"{where}: 数値の欄に真偽値があります: {value!r}")
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as error:
        raise CodecError(f"{where}: 数値として読めません: {value!r}") from error
    if not number.is_finite():
        raise CodecError(f"{where}: 数値として読めません: {value!r}")
    return number


def _number_cell(value: Any) -> Any:
    """Decimal などをシートに書く数値（int か float）にする。"""
    if value is None or value == "":
        return ""
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def parse_boolean(value: Any) -> bool:
    """Helpers.gs の parseBoolean_: String(value).toLowerCase() === 'true' || value === true。"""
    return value is True or js_string(value).lower() == "true"


# ===== 読み込み（シート → DB） =====

@dataclass
class DecodeContext:
    sheet_tz: tzinfo = JST
    warnings: list = field(default_factory=list)


def _decode_value(spec: TableSpec, column: str, value: Any, formatted: Any, row_no: int,
                  ctx: DecodeContext) -> Any:
    kind = spec.kind(column)
    where = f"{spec.sheet} {row_no}行目 {column}"

    if kind in ("text", "date_text", "version"):
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=ctx.sheet_tz)
            text = dt.astimezone(JST).strftime("%Y-%m-%d")
            ctx.warnings.append(f"{where}: 日付のセルを文字 {text} にしました（元は日付として保存）")
            return text
        if isinstance(value, date):
            text = value.strftime("%Y-%m-%d")
            ctx.warnings.append(f"{where}: 日付のセルを文字 {text} にしました（元は日付として保存）")
            return text
        is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
        if is_number and (kind == "date_text" or _looks_like_date_display(formatted)):
            # 日付のセル（シリアル値）。GAS 版の normalizeCellValue_ と同じく日本時間の yyyy-MM-dd にする。
            text = serial_to_datetime(value, ctx.sheet_tz).strftime("%Y-%m-%d")
            if kind != "date_text":
                ctx.warnings.append(
                    f"{where}: 日付のセル（表示「{formatted}」）を文字 {text} にしました。"
                    "Migration.gs の reportDateCorruptedText() で確かめてください")
            return text
        return js_string(value)

    if kind == "datetime":
        if is_blank(value):
            return None
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=ctx.sheet_tz)).astimezone(JST)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=JST)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return serial_to_datetime(value, ctx.sheet_tz)
        parsed = parse_datetime_text(str(value), ctx.sheet_tz)
        if parsed is None:
            raise CodecError(f"{where}: 日時として読めません: {value!r}")
        return parsed

    if kind == "date":
        if is_blank(value):
            return None
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=ctx.sheet_tz)
            return dt.astimezone(JST).date()
        if isinstance(value, date):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return serial_to_datetime(value, ctx.sheet_tz).date()
        parsed = parse_datetime_text(str(value), ctx.sheet_tz)
        if parsed is None:
            raise CodecError(f"{where}: 日付として読めません: {value!r}")
        return parsed.date()

    if kind == "number":
        return _to_decimal(value, where)

    if kind == "int":
        number = _to_decimal(value, where)
        if number is None:
            return None
        if number != number.to_integral_value():
            raise CodecError(f"{where}: 整数ではありません: {value!r}")
        return int(number)

    if kind == "bool_false":
        return parse_boolean(value)

    if kind == "bool_true":
        return True if is_blank(value) else parse_boolean(value)

    text = js_string(value).strip() if not isinstance(value, str) else value.strip()

    if kind in ("status", "status_opt"):
        if kind == "status_opt" and text == "":
            return ""
        if text not in STATUS_VALUES:
            raise CodecError(f"{where}: 決まった状態の値にありません: {value!r}")
        return text
    if kind in ("step", "step_opt"):
        if kind == "step_opt" and text == "":
            return ""
        if text not in STEP_VALUES:
            raise CodecError(f"{where}: 決まった段の値にありません: {value!r}")
        return text
    if kind == "action":
        if text not in ACTION_VALUES:
            raise CodecError(f"{where}: 決まった操作の値にありません: {value!r}")
        return text
    if kind == "reason":
        if text != "" and text not in REASON_CODES:
            raise CodecError(f"{where}: 決まった理由コードにありません: {value!r}")
        return text
    if kind == "category":
        if text == "":
            return CATEGORY_DEFAULT
        if text in CATEGORY_VALUES:
            return text
        if text.upper() in CATEGORY_LEGACY:
            return CATEGORY_LEGACY[text.upper()]
        raise CodecError(f"{where}: 決まった品目区分にありません: {value!r}")
    if kind == "recipient_type":
        if text not in RECIPIENT_TYPE_VALUES:
            raise CodecError(f"{where}: 決まった通知宛先の種別にありません: {value!r}")
        return text
    if kind == "send_as":
        # Recipients.gs: String(row.sendAs).toUpperCase() === 'CC' ? 'CC' : 'TO'
        return "CC" if text.upper() == "CC" else "TO"
    if kind == "route":
        if text == "":
            return []
        try:
            route = json.loads(text)
        except json.JSONDecodeError as error:
            raise CodecError(f"{where}: 承認経路が JSON として読めません: {value!r}") from error
        if not isinstance(route, list) or any(step not in STEP_VALUES for step in route):
            raise CodecError(f"{where}: 承認経路に決まった段の値でないものがあります: {value!r}")
        return route
    if kind == "setting":
        return js_string(value)
    raise AssertionError(f"unknown kind {kind}")


def sheet_row_to_db(table: str, row: list, formatted_row: Optional[list] = None, row_no: int = 0,
                    ctx: Optional[DecodeContext] = None) -> dict:
    """シートの 1 行（Constants.gs の列の並び）→ DB 用の dict（snake_case のキー）。

    row: UNFORMATTED_VALUE + SERIAL_NUMBER の値（または xlsx / CSV の値）。足りない列は '' とみなす。
    formatted_row: 同じ行の FORMATTED_VALUE（あれば日付に化けた自由入力を見分ける）。
    row_no: シートの行番号（エラー文と sort_order / seq に使う。2 行目が最初のデータ）。
    """
    spec = TABLES[table]
    ctx = ctx or DecodeContext()
    result = {}
    for index, column in enumerate(spec.columns):
        value = row[index] if index < len(row) else ""
        formatted = formatted_row[index] if formatted_row is not None and index < len(formatted_row) else None
        result[db_key(column)] = _decode_value(spec, column, value, formatted, row_no, ctx)
    if spec.order_key:
        result[spec.order_key] = row_no - 1 if row_no else 0
    return result


def row_is_empty(row: Iterable) -> bool:
    return all(is_blank(value) or (isinstance(value, str) and value.strip() == "") for value in row)


# ===== 書き出し（DB → シート、valueInputOption=RAW） =====

def _encode_value(spec: TableSpec, column: str, value: Any) -> Any:
    kind = spec.kind(column)
    if kind == "datetime":
        return format_datetime(value)
    if kind == "date":
        if value is None or value == "":
            return ""
        if isinstance(value, datetime):
            return to_jst(value).strftime("%Y-%m-%d")
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        return str(value)
    if kind in ("number", "int"):
        return _number_cell(value)
    if kind in ("bool_false", "bool_true"):
        if value is None or value == "":
            return ""
        return "true" if parse_boolean(value) else "false"
    if kind == "route":
        if value is None or value == "":
            return ""
        route = json.loads(value) if isinstance(value, str) else value
        return json.dumps(route, ensure_ascii=False, separators=(",", ":"))
    if kind == "version":
        return APP_VERSION
    if kind == "send_as":
        return "CC" if js_string(value).upper() == "CC" else "TO"
    return js_string(value)


def db_to_sheet_row(table: str, record: dict) -> list:
    """DB の dict（snake_case のキー）→ シートに RAW で書く 1 行（Constants.gs の列の並び）。"""
    spec = TABLES[table]
    return [_encode_value(spec, column, record.get(db_key(column))) for column in spec.columns]


def db_rows_to_sheet_values(table: str, records: list) -> list:
    """表の全行をシートの値（見出しを除く）にする。sort_order / seq があればその順に並べる。"""
    spec = TABLES[table]
    rows = list(records)
    if spec.order_key and all(spec.order_key in r and r[spec.order_key] is not None for r in rows):
        rows.sort(key=lambda r: r[spec.order_key])
    return [db_to_sheet_row(table, r) for r in rows]
