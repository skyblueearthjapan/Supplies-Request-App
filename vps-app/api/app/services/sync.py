"""作業員マスタ・部署別上席マスタの同期（MasterData.gs / DeptSupervisors.gs の移植）。

外部のスプレッドシートは委任トークンで読むだけ。DB には依存しない。
結果の rows（DB 用の dict のリスト）で表を丸ごと置き換えるのは呼ぶ側（worker・RPC）の仕事。
rows が None のときは「上書きしない」（元が空・有効な行がゼロ・読めない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.services import sheet_codec as codec
from app.services.sheets import SheetsApi, quote_sheet

# Constants.gs
EXTERNAL_MASTER_SPREADSHEET_ID = "1iu5HoaknlW1W1HheeYv0jqcRq-aY0SyEE2seQd2pHkQ"
MASTER_WORKER_GID = 684189184
DEPT_APPROVER_SPREADSHEET_ID = "1Knx_kaQMZZams65J1oeSDaBeWUt8XXanNe94XSAHKFQ"
DEPT_APPROVER_SHEET_NAME = "DeptApprovers"

INACTIVE_VALUES = frozenset({"false", "0", "no", "×", "無効"})


def sanitize_text(value: Any, max_length: int = 0) -> str:
    """Helpers.gs の sanitizeText_: String(value).trim() を max_length で切る。"""
    text = codec.js_string(value).strip()
    return text[:max_length] if max_length and len(text) > max_length else text


def normalize_email(value: Any) -> str:
    return sanitize_text(value, 256).lower()


@dataclass
class SyncResult:
    ok: bool
    count: int
    synced_at: str                 # 日本時間 yyyy-MM-dd HH:mm:ss
    rows: Optional[list] = None    # None なら DB を上書きしない
    skipped: bool = False
    error: str = ""
    warnings: list = field(default_factory=list)

    def to_client(self) -> dict:
        """GAS 版の syncWorkersMaster / syncDeptSupervisorsMaster の戻り値と同じ形。"""
        result: dict = {"count": self.count}
        if self.skipped:
            result["skipped"] = True
        if self.error:
            result["error"] = self.error
        return result


def _now(now: Optional[datetime]) -> str:
    return (now or datetime.now(codec.JST)).astimezone(codec.JST).strftime("%Y-%m-%d %H:%M:%S")


# ===== 作業員マスタ =====

def parse_workers(values: list) -> list:
    """外部の作業員マスタのタブ（2 行目から。A〜F 列）→ workers の dict。A・B・C・E・F 列を位置で読む。"""
    rows = []
    for raw in values:
        cells = list(raw) + [""] * (6 - len(raw))
        if cells[0] is None or cells[0] == "" or cells[0] is False or cells[0] == 0:
            continue  # GAS: filter(r => r[0])
        rows.append({
            "worker_code": sanitize_text(cells[0], 100),  # A 作業員コード
            "name": sanitize_text(cells[1], 100),         # B 氏名
            "dept": sanitize_text(cells[2], 100),         # C 部署
            "location": sanitize_text(cells[4], 100),     # E 拠点
            "staff_type": sanitize_text(cells[5], 100),   # F スタッフ種類
            "sort_order": len(rows) + 1,
        })
    return rows


def sync_workers(api: SheetsApi, now: Optional[datetime] = None) -> SyncResult:
    stamp = _now(now)
    try:
        meta = api.get_spreadsheet(EXTERNAL_MASTER_SPREADSHEET_ID)
        title = next((s["properties"]["title"] for s in meta.get("sheets", [])
                      if s["properties"].get("sheetId") == MASTER_WORKER_GID), None)
        if title is None:
            return SyncResult(False, 0, stamp, error="source sheet not found")
        values = api.get_values(EXTERNAL_MASTER_SPREADSHEET_ID, f"{quote_sheet(title)}!A2:F")
    except Exception as error:  # 読めないときは上書きしない
        return SyncResult(False, 0, stamp, error=str(error))
    if not values:
        return SyncResult(True, 0, stamp, skipped=True)  # 元が空なら今の DB を残す
    rows = parse_workers(values)
    if not rows:
        return SyncResult(True, 0, stamp, skipped=True)
    return SyncResult(True, len(rows), stamp, rows=rows)


# ===== 部署別上席マスタ（DeptApprovers） =====

def find_header_index(header: list, candidates: list) -> int:
    """DeptSupervisors.gs の findHeaderIndex_: 見出しを部分一致で探す（列の順に、候補の順に）。"""
    for index, label in enumerate(header):
        for candidate in candidates:
            if candidate.lower() in label:
                return index
    return -1


def pick_dept_approver_sheet(titles: list) -> Optional[str]:
    if DEPT_APPROVER_SHEET_NAME in titles:
        return DEPT_APPROVER_SHEET_NAME
    for title in titles:  # 名称ゆれ（ApproverMap など）
        if "approver" in str(title or "").lower():
            return title
    return None


def parse_dept_approvers(values: list) -> list:
    """DeptApprovers のタブ全体（1 行目が見出し）→ dept_supervisors の dict。"""
    width = max((len(r) for r in values), default=0)
    grid = [list(r) + [""] * (width - len(r)) for r in values]
    header = [codec.js_string(h).strip().lower() for h in grid[0]]
    idx = {
        "dept": find_header_index(header, ["部署", "dept", "department"]),
        "email": find_header_index(header, ["承認者メール", "メール", "mail", "email", "approveremail", "approver"]),
        "name": find_header_index(header, ["氏名", "名前", "approvername", "name"]),
        "title": find_header_index(header, ["役職", "title", "position"]),
        "active": find_header_index(header, ["有効", "active", "enabled"]),
        "notify": find_header_index(header, ["通知メール", "通知先", "通知", "notifyemail", "notify", "送信先"]),
    }
    if idx["dept"] < 0:
        idx["dept"] = 0
    if idx["email"] < 0:
        idx["email"] = 1

    def cell(row: list, key: str) -> Any:
        i = idx[key]
        return row[i] if 0 <= i < len(row) else ""

    rows = []
    for row in grid[1:]:
        dept = sanitize_text(cell(row, "dept"), 100)
        if not dept:
            continue
        if idx["active"] >= 0 and codec.js_string(cell(row, "active")).strip().lower() in INACTIVE_VALUES:
            continue
        name = sanitize_text(cell(row, "name"), 100) if idx["name"] >= 0 else ""
        title = sanitize_text(cell(row, "title"), 40) if idx["title"] >= 0 else ""
        notify = normalize_email(cell(row, "notify")) if idx["notify"] >= 0 else ""
        emails_raw = codec.js_string(cell(row, "email"))
        for part in _split_emails(emails_raw):
            email = normalize_email(part)
            if email:
                # 1 行に複数メールがある場合、通知メールは同じ値を各行へ写す。
                rows.append({
                    "department": dept, "email": email, "name": name, "active": True,
                    "title": title, "notify_email": notify, "sort_order": len(rows) + 1,
                })
    return rows


def _split_emails(text: str) -> list:
    return re.split(r"[,;\n]", text)


def sync_dept_supervisors(api: SheetsApi, now: Optional[datetime] = None) -> SyncResult:
    stamp = _now(now)
    try:
        meta = api.get_spreadsheet(DEPT_APPROVER_SPREADSHEET_ID)
        title = pick_dept_approver_sheet([s["properties"]["title"] for s in meta.get("sheets", [])])
        if title is None:
            return SyncResult(False, 0, stamp, error="source sheet not found")
        values = api.get_values(DEPT_APPROVER_SPREADSHEET_ID, quote_sheet(title))
    except Exception as error:
        return SyncResult(False, 0, stamp, error=str(error))
    if len(values) < 2:
        return SyncResult(True, 0, stamp, skipped=True)
    rows = parse_dept_approvers(values)
    if not rows:
        return SyncResult(True, 0, stamp, skipped=True)
    return SyncResult(True, len(rows), stamp, rows=rows)
