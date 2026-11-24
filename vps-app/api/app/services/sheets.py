"""Google Sheets API の読み書き（委任トークン）。

- 読み込み: 移行用。読むだけ。見出しの文字で列を照合し、位置には頼らない。
- 書き出し: 全タブの差し替え（毎晩。戻せる期間は毎時）。書き出し先は今のスプレッドシートの今のタブ。
  行数が減ったら余りの行を消す。値は sheet_codec が作る RAW の値で書く。
  書き出しの前に、毎回 safety.assert_writable_spreadsheet で書き出し先を確かめる
  （staging では本番の ID に書かない）。

export_all_tables / read_all_tables は DB には依存しない。書き出しは「表名 → dict のリスト」を受け取り、
読み込みは同じ形を返す。worker の定期処理 run_scheduled_export だけが DB を読み、SyncRun に記録する。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Callable, Optional

from app.services import sheet_codec as codec

try:  # 担当 F の安全装置。まだ無いときだけ下の仮の関数を使う。
    from app.safety import assert_writable_spreadsheet  # type: ignore
except ImportError:  # pragma: no cover - F の土台とまとめたら消える
    def assert_writable_spreadsheet(spreadsheet_id: str) -> None:
        """仮の安全装置（担当 F の safety.py とまとめるまで）。

        APP_ENV が production でなければ、本番のスプレッドシートへの書き込みを止める。
        """
        if not spreadsheet_id:
            raise RuntimeError("書き出し先のスプレッドシート ID が未設定です。")
        if spreadsheet_id == codec.LEGACY_SPREADSHEET_ID:
            raise RuntimeError("旧 DB（アーカイブ）には書き込みません。")
        if os.environ.get("APP_ENV", "staging") != "production" and spreadsheet_id == codec.PRODUCTION_SPREADSHEET_ID:
            raise RuntimeError("APP_ENV が production ではないため、本番のスプレッドシートには書き込みません。")

log = logging.getLogger(__name__)

SCOPE_READONLY = "https://www.googleapis.com/auth/spreadsheets.readonly"
SCOPE_READWRITE = "https://www.googleapis.com/auth/spreadsheets"
API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(RuntimeError):
    pass


class SheetsApi:
    """Sheets API v4 の薄い包み。token_provider は委任トークン（文字）を返す関数。

    担当 F の google_delegate から、必要なスコープのアクセストークンを返す関数を渡す。
    """

    def __init__(self, token_provider: Callable[[], str], timeout: float = 60.0, retries: int = 4):
        self._token = token_provider
        self._timeout = timeout
        self._retries = retries

    def _call(self, method: str, url: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(self._retries + 1):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", "Bearer " + self._token())
            if data is not None:
                request.add_header("Content-Type", "application/json; charset=utf-8")
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    payload = response.read()
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as error:
                if error.code in (429, 500, 502, 503, 504) and attempt < self._retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                detail = error.read().decode("utf-8", "replace")[:500]
                raise SheetsError(f"Sheets API {method} が失敗しました（{error.code}）: {detail}") from error
        raise SheetsError("Sheets API の再試行が尽きました。")  # pragma: no cover

    def get_spreadsheet(self, spreadsheet_id: str) -> dict:
        fields = "properties(title,timeZone,locale),sheets(properties(sheetId,title,gridProperties))"
        url = f"{API_BASE}/{spreadsheet_id}?fields={urllib.parse.quote(fields)}"
        return self._call("GET", url)

    def get_values(self, spreadsheet_id: str, a1: str, render: str = "UNFORMATTED_VALUE") -> list:
        query = urllib.parse.urlencode({
            "valueRenderOption": render,
            "dateTimeRenderOption": "SERIAL_NUMBER",
            "majorDimension": "ROWS",
        })
        url = f"{API_BASE}/{spreadsheet_id}/values/{urllib.parse.quote(a1)}?{query}"
        return self._call("GET", url).get("values", [])

    def batch_update_values(self, spreadsheet_id: str, data: list) -> dict:
        url = f"{API_BASE}/{spreadsheet_id}/values:batchUpdate"
        return self._call("POST", url, {"valueInputOption": "RAW", "data": data})

    def batch_clear(self, spreadsheet_id: str, ranges: list) -> dict:
        url = f"{API_BASE}/{spreadsheet_id}/values:batchClear"
        return self._call("POST", url, {"ranges": ranges})


# ===== 小道具 =====

def quote_sheet(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def column_letter(index: int) -> str:
    """0 始まりの列番号 → A, B, ..., Z, AA。"""
    letters = ""
    n = index + 1
    while n:
        n, rest = divmod(n - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def sheet_timezone(meta: dict) -> tzinfo:
    name = (meta.get("properties") or {}).get("timeZone") or "Asia/Tokyo"
    if name in ("Asia/Tokyo", "Japan"):
        return codec.JST
    from zoneinfo import ZoneInfo  # Windows では tzdata が要る

    return ZoneInfo(name)


def sheet_titles(meta: dict) -> list:
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def match_headers(spec: codec.TableSpec, header_row: list) -> dict:
    """見出しの文字で列を照合する。戻り値は 内部キー → シートの列番号（0 始まり）。

    決まった見出しが 1 つでも無い・同じ見出しが 2 つある場合は止める。
    """
    positions: dict = {}
    for index, cell in enumerate(header_row):
        label = codec.js_string(cell).strip()
        if label == "":
            continue
        if label in positions:
            raise SheetsError(f"{spec.sheet}: 見出し「{label}」が 2 つあります。")
        positions[label] = index
    missing = [h for h in spec.headers if h not in positions]
    if missing:
        raise SheetsError(f"{spec.sheet}: 見出しが見つかりません: {', '.join(missing)}")
    return {column: positions[header] for column, header in zip(spec.columns, spec.headers)}


@dataclass
class TableRead:
    table: str
    rows: list                      # DB 用の dict
    header_count: int               # シートの見出しの数
    extra_headers: list             # 決まりに無い見出し
    warnings: list = field(default_factory=list)
    skipped_empty_rows: int = 0


def decode_table(table: str, values: list, formatted: Optional[list] = None,
                 sheet_tz: tzinfo = codec.JST) -> TableRead:
    """1 行目が見出しの 2 次元配列（API・xlsx・CSV 共通）→ DB 用の dict のリスト。"""
    spec = codec.TABLES[table]
    if not values:
        raise SheetsError(f"{spec.sheet}: 見出しの行がありません。")
    header_row = values[0]
    positions = match_headers(spec, header_row)
    known = set(spec.headers)
    extra = [codec.js_string(h).strip() for h in header_row
             if codec.js_string(h).strip() and codec.js_string(h).strip() not in known]
    ctx = codec.DecodeContext(sheet_tz=sheet_tz)
    rows = []
    skipped = 0
    for offset, raw in enumerate(values[1:]):
        row_no = offset + 2
        aligned = [raw[positions[c]] if positions[c] < len(raw) else "" for c in spec.columns]
        if codec.row_is_empty(aligned):
            skipped += 1
            continue
        fmt_aligned = None
        if formatted is not None:
            fraw = formatted[offset + 1] if offset + 1 < len(formatted) else []
            fmt_aligned = [fraw[positions[c]] if positions[c] < len(fraw) else "" for c in spec.columns]
        rows.append(codec.sheet_row_to_db(table, aligned, fmt_aligned, row_no, ctx))
    header_count = len([h for h in header_row if codec.js_string(h).strip()])
    if header_count != len(spec.headers):
        ctx.warnings.append(f"{spec.sheet}: 見出しの数が {header_count}（決まりは {len(spec.headers)}）です。")
    return TableRead(table, rows, header_count, extra, ctx.warnings, skipped)


# ===== 読み込み（移行用。読むだけ） =====

def read_all_tables(api: SheetsApi, spreadsheet_id: str, tables: Optional[list] = None) -> dict:
    """スプレッドシートの 8 タブを読み、表名 → TableRead を返す。何も書かない。"""
    meta = api.get_spreadsheet(spreadsheet_id)
    titles = sheet_titles(meta)
    tz = sheet_timezone(meta)
    result = {}
    for table in tables or codec.EXPORT_ORDER:
        spec = codec.TABLES[table]
        if spec.sheet not in titles:
            raise SheetsError(f"タブ「{spec.sheet}」がありません（旧 DB や別のシートを指していないか確かめてください）。")
        a1 = quote_sheet(spec.sheet)
        values = api.get_values(spreadsheet_id, a1, "UNFORMATTED_VALUE")
        formatted = api.get_values(spreadsheet_id, a1, "FORMATTED_VALUE")
        result[table] = decode_table(table, values, formatted, tz)
    return result


# ===== 書き出し（全タブの差し替え） =====

@dataclass
class ExportResult:
    spreadsheet_id: str
    finished_at: str
    counts: dict        # 表名 → 書いた行数


def export_all_tables(api: SheetsApi, spreadsheet_id: str, tables: dict,
                      now: Optional[datetime] = None) -> ExportResult:
    """DB の全行（表名 → dict のリスト）で、スプレッドシートの各タブを差し替える。

    - 書く前に毎回 assert_writable_spreadsheet で書き出し先を確かめる。
    - 1 行目の見出しを文字で照合し、Constants.gs の並びと違えば止める（GAS 版は位置で読むため）。
      見出しが空のタブには見出しを書く。
    - 先にデータを書き、その後で余りの行（前回より減った分）を消す。途中で失敗しても行が減りすぎない。
    - 決まりの列より右の列には触れない。
    """
    assert_writable_spreadsheet(spreadsheet_id)
    meta = api.get_spreadsheet(spreadsheet_id)
    titles = sheet_titles(meta)
    counts = {}
    for table in codec.EXPORT_ORDER:
        if table not in tables:
            continue
        spec = codec.TABLES[table]
        if spec.sheet not in titles:
            raise SheetsError(f"書き出し先にタブ「{spec.sheet}」がありません。")
        last_col = column_letter(len(spec.columns) - 1)
        header = api.get_values(spreadsheet_id, f"{quote_sheet(spec.sheet)}!A1:{last_col}1", "FORMATTED_VALUE")
        header_row = [codec.js_string(h).strip() for h in (header[0] if header else [])]
        header_row += [""] * (len(spec.headers) - len(header_row))
        if any(header_row) and header_row != spec.headers:
            raise SheetsError(f"{spec.sheet}: 見出しの並びが決まり（Constants.gs）と違うため書き出しを止めました。")

        values = codec.db_rows_to_sheet_values(table, tables[table])
        data = [{"range": f"{quote_sheet(spec.sheet)}!A1:{last_col}1", "values": [spec.headers]}]
        if values:
            data.append({
                "range": f"{quote_sheet(spec.sheet)}!A2:{last_col}{len(values) + 1}",
                "values": values,
            })
        assert_writable_spreadsheet(spreadsheet_id)
        api.batch_update_values(spreadsheet_id, data)
        assert_writable_spreadsheet(spreadsheet_id)
        api.batch_clear(spreadsheet_id, [f"{quote_sheet(spec.sheet)}!A{len(values) + 2}:{last_col}"])
        counts[table] = len(values)
    finished = (now or datetime.now(codec.JST)).astimezone(codec.JST).strftime("%Y-%m-%d %H:%M:%S")
    return ExportResult(spreadsheet_id, finished, counts)


# ===== worker の定期処理（毎晩。戻せる期間は毎時） =====

EXPORT_SYNC_KIND = "export"

# シートに書く行の並び（sort_order / seq を持つ表は sheet_codec が並べ直す）
_EXPORT_ORDER_BY = {
    "requests": ("created_at", "request_id"),
    "request_items": ("request_id", "line_no", "item_id"),
    "history": ("happened_at", "seq"),
    "settings": ("key",),
}


def read_db_tables(db) -> dict:
    """DB の 8 表を「表名 → DB 用の dict（列名がキー）のリスト」で読む。"""
    from sqlalchemy import select

    from app import models  # noqa: F401 - すべての表を読み込む
    from app.db import Base

    result = {}
    for table in codec.EXPORT_ORDER:
        target = Base.metadata.tables[table]
        keys = _EXPORT_ORDER_BY.get(table) or ("sort_order", "id")
        query = select(target).order_by(*[target.c[k] for k in keys])
        result[table] = [dict(row._mapping) for row in db.execute(query)]
    return result


def run_scheduled_export(session_factory) -> Optional[ExportResult]:
    """DB の全表を safety.export_spreadsheet_id()（staging はステージング用、production は本番）へ書き出す。

    全タブの差し替えなので、何度動いても同じ結果になる。SyncRun に kind=export で記録する。
    委任がまだ無ければ失敗として記録して飛ばす。それ以外の失敗は記録した上で例外を上げる（worker がログに出す）。
    """
    from app import safety
    from app.config import get_settings
    from app.domain import admin
    from app.google_delegate import DelegateNotAuthorized
    from app.models import SyncRun
    from app.timeutil import now_utc

    settings = get_settings()
    started = now_utc()
    result: Optional[ExportResult] = None
    failure: Optional[Exception] = None
    message = ""
    try:
        target = safety.export_spreadsheet_id(settings)
        with session_factory() as db:
            api = admin._sheets_api(db, settings)
            tables = read_db_tables(db)
        result = export_all_tables(api, target, tables)
        message = target + ": " + ", ".join(f"{t}={n}" for t, n in result.counts.items())
    except DelegateNotAuthorized as error:
        message = str(error)
    except Exception as error:  # noqa: BLE001 - 記録してから上げ直す
        failure = error
        message = f"{type(error).__name__}: {error}"
    with session_factory() as db:
        db.add(SyncRun(kind=EXPORT_SYNC_KIND, started_at=started, finished_at=now_utc(), ok=result is not None,
                       row_count=sum(result.counts.values()) if result else 0, message=message[:2000]))
        db.commit()
    if failure is not None:
        raise failure
    if result is None:
        log.warning("scheduled export skipped: %s", message)
    return result
