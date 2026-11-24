import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import sheet_codec as codec  # noqa: E402
from app.services import sheets, sync  # noqa: E402


class FakeSheetsApi:
    """Sheets API の代わり。tabs: タイトル → 2 次元配列（1 行目から）。"""

    def __init__(self, tabs, gids=None, time_zone="Asia/Tokyo", formatted=None):
        self.tabs = {k: [list(r) for r in v] for k, v in tabs.items()}
        self.gids = gids or {}
        self.time_zone = time_zone
        self.formatted = formatted or {}
        self.updates = []
        self.clears = []

    def get_spreadsheet(self, spreadsheet_id):
        return {
            "properties": {"timeZone": self.time_zone},
            "sheets": [{"properties": {"title": t, "sheetId": self.gids.get(t, i)}} for i, t in enumerate(self.tabs)],
        }

    def _parse(self, a1):
        m = re.match(r"^'((?:[^']|'')*)'(?:!([A-Z]+)(\d+)(?::([A-Z]+)(\d*))?)?$", a1)
        title = m.group(1).replace("''", "'")
        start_row = int(m.group(3) or 1)
        end_row = int(m.group(5)) if m.group(5) else None
        return title, start_row, end_row

    def get_values(self, spreadsheet_id, a1, render="UNFORMATTED_VALUE"):
        title, start, end = self._parse(a1)
        source = self.formatted.get(title) if render == "FORMATTED_VALUE" and title in self.formatted else self.tabs[title]
        rows = source[start - 1:end]
        while rows and all(v in ("", None) for v in rows[-1]):
            rows.pop()
        return [list(r) for r in rows]

    def batch_update_values(self, spreadsheet_id, data):
        self.updates.append(data)
        for item in data:
            title, start, _ = self._parse(item["range"])
            grid = self.tabs[title]
            for offset, values in enumerate(item["values"]):
                index = start - 1 + offset
                while len(grid) <= index:
                    grid.append([])
                grid[index] = list(values) + grid[index][len(values):]

    def batch_clear(self, spreadsheet_id, ranges):
        self.clears.append(ranges)
        for a1 in ranges:
            title, start, _ = self._parse(a1)
            del self.tabs[title][start - 1:]


# ===== sheets: 読み込み =====

def test_decode_table_matches_headers_by_text_not_position():
    spec = codec.TABLES["notification_recipients"]
    header = list(reversed(spec.headers)) + ["メモ"]
    row = list(reversed(["購買", "p@x", "購買さん", "", "CC"])) + ["自由"]
    read = sheets.decode_table("notification_recipients", [header, row, ["", "", "", "", "", ""]])
    assert read.rows == [{"type": "購買", "email": "p@x", "name": "購買さん", "active": True,
                          "send_as": "CC", "sort_order": 1}]
    assert read.extra_headers == ["メモ"]
    assert read.skipped_empty_rows == 1
    assert any("見出しの数" in w for w in read.warnings)


def test_decode_table_stops_on_missing_or_duplicate_header():
    spec = codec.TABLES["dept_supervisors"]
    with pytest.raises(sheets.SheetsError):
        sheets.decode_table("dept_supervisors", [spec.headers[:-1]])
    with pytest.raises(sheets.SheetsError):
        sheets.decode_table("dept_supervisors", [spec.headers + ["部署"]])
    with pytest.raises(sheets.SheetsError):  # 旧 DB の英語の見出し
        sheets.decode_table("dept_supervisors", [codec.DEPT_SUPERVISOR_COLUMNS])


def test_read_all_tables_uses_formatted_values_to_find_date_cells():
    tabs = {codec.TABLES[t].sheet: [codec.TABLES[t].headers] for t in codec.EXPORT_ORDER}
    serial = (datetime(2026, 3, 8) - datetime(1899, 12, 30)).days
    tabs["申請明細"].append(["ITEM-1", "REQ-1", 1, "配管", serial, "", 1, 1, 1, "", "", ""])
    formatted = {"申請明細": [codec.ITEM_HEADERS, ["ITEM-1", "REQ-1", "1", "配管", "3/8/2026", "", "1", "1", "1", "", "", ""]]}
    reads = sheets.read_all_tables(FakeSheetsApi(tabs, formatted=formatted), "SRC")
    assert reads["request_items"].rows[0]["model"] == "2026-03-08"
    assert reads["request_items"].warnings


# ===== sheets: 書き出し =====

def export_tabs():
    tabs = {codec.TABLES[t].sheet: [codec.TABLES[t].headers] for t in codec.EXPORT_ORDER}
    tabs["申請明細"] += [["OLD-%d" % i] + [""] * 11 for i in range(5)]
    return tabs


def item(n):
    return {"item_id": f"ITEM-{n}", "request_id": "REQ-1", "line_no": n, "name": "3/8", "model": "0123",
            "maker": "=1+1", "quantity": 2, "unit_price": 100, "amount": 200, "desired_delivery_date": "",
            "note": "1/2", "unit": "個"}


def test_export_replaces_tab_and_clears_leftover_rows(monkeypatch):
    monkeypatch.setattr(sheets, "assert_writable_spreadsheet", lambda sid: None)
    api = FakeSheetsApi(export_tabs())
    result = sheets.export_all_tables(api, "STAGING", {"request_items": [item(1), item(2)]})
    assert result.counts == {"request_items": 2}
    grid = api.tabs["申請明細"]
    assert grid[0] == codec.ITEM_HEADERS
    assert grid[1] == ["ITEM-1", "REQ-1", 1, "3/8", "0123", "=1+1", 2, 100, 200, "", "1/2", "個"]
    assert len(grid) == 3  # 余りの OLD 行は消えた
    assert api.clears == [["'申請明細'!A4:L"]]


def test_export_stops_when_header_differs(monkeypatch):
    monkeypatch.setattr(sheets, "assert_writable_spreadsheet", lambda sid: None)
    tabs = export_tabs()
    tabs["申請明細"][0] = list(reversed(codec.ITEM_HEADERS))
    api = FakeSheetsApi(tabs)
    with pytest.raises(sheets.SheetsError):
        sheets.export_all_tables(api, "STAGING", {"request_items": [item(1)]})
    assert api.updates == []


def test_export_refuses_production_spreadsheet_in_staging(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    api = FakeSheetsApi(export_tabs())
    with pytest.raises(Exception):
        sheets.export_all_tables(api, codec.PRODUCTION_SPREADSHEET_ID, {"request_items": [item(1)]})
    assert api.updates == [] and api.clears == []


def test_export_checks_target_before_every_write(monkeypatch):
    calls = []
    monkeypatch.setattr(sheets, "assert_writable_spreadsheet", lambda sid: calls.append(sid))
    api = FakeSheetsApi(export_tabs())
    sheets.export_all_tables(api, "STAGING", {"request_items": [item(1)], "workers": []})
    assert calls == ["STAGING"] * 5  # 最初 + 表ごとに書く前・消す前


def test_column_letter():
    assert [sheets.column_letter(i) for i in (0, 11, 22, 25, 26)] == ["A", "L", "W", "Z", "AA"]


# ===== sync: 作業員マスタ =====

def test_parse_workers_reads_a_b_c_e_f_by_position():
    rows = sync.parse_workers([
        [123.0, " 山田 ", "機械部", "D は読まない", "本社", "社員"],
        ["", "コード無し", "", "", "", ""],
        ["0456", "鈴木", "電気部"],
    ])
    assert rows == [
        {"worker_code": "123", "name": "山田", "dept": "機械部", "location": "本社", "staff_type": "社員", "sort_order": 1},
        {"worker_code": "0456", "name": "鈴木", "dept": "電気部", "location": "", "staff_type": "", "sort_order": 2},
    ]


def test_sync_workers_finds_tab_by_gid_and_keeps_db_when_source_empty():
    api = FakeSheetsApi({"一覧": [["x"], ["y"]], "作業員": [["コード"], ["0001", "山田", "機械部", "", "本社", "社員"]]},
                        gids={"作業員": sync.MASTER_WORKER_GID})
    result = sync.sync_workers(api, now=datetime(2026, 11, 24, 8, 0, tzinfo=codec.JST))
    assert result.ok and result.count == 1 and result.rows[0]["worker_code"] == "0001"
    assert result.synced_at == "2026-11-24 08:00:00"
    assert result.to_client() == {"count": 1}

    empty = sync.sync_workers(FakeSheetsApi({"作業員": [["コード"]]}, gids={"作業員": sync.MASTER_WORKER_GID}))
    assert empty.ok and empty.skipped and empty.rows is None
    assert empty.to_client() == {"count": 0, "skipped": True}

    missing = sync.sync_workers(FakeSheetsApi({"別": [["a"]]}))
    assert not missing.ok and missing.rows is None and missing.error == "source sheet not found"


# ===== sync: 部署別上席マスタ =====

def test_dept_approvers_partial_header_match_and_quirks():
    values = [
        ["部署名", "承認者メールアドレス", "承認者の氏名", "役職名", "有効フラグ", "通知メール（任意）"],
        ["機械部", "A@x, b@x; c@x", "山田", "課長", "", "N@x"],
        ["電気部", "d@x", "", "", "無効", ""],
        ["総務部", "e@x", "", "", "×", ""],
        ["購買部", "f@x", "", "", "FALSE", ""],
        ["", "g@x", "", "", "", ""],
        ["品質部", "", "", "", "", ""],
        ["*", "h@x", "全社", "", 0, ""],
        ["検査部", "i@x", "", "", "true", ""],
    ]
    rows = sync.parse_dept_approvers(values)
    assert [(r["department"], r["email"], r["notify_email"], r["sort_order"]) for r in rows] == [
        ("機械部", "a@x", "n@x", 1), ("機械部", "b@x", "n@x", 2), ("機械部", "c@x", "n@x", 3),
        ("検査部", "i@x", "", 4),
    ]
    assert rows[0]["name"] == "山田" and rows[0]["title"] == "課長" and rows[0]["active"] is True


def test_dept_approvers_fallback_to_columns_a_and_b():
    rows = sync.parse_dept_approvers([["列1", "列2", "列3"], ["機械部", "a@x", "x"]])
    assert [(r["department"], r["email"], r["name"]) for r in rows] == [("機械部", "a@x", "")]


def test_dept_approvers_header_candidate_order_is_same_as_gas():
    # GAS 版と同じく、メールの候補 'approver' は「approver name」にも当たる（列の左から探す）
    header = ["dept", "approver name", "email"]
    idx = sync.find_header_index([h.lower() for h in header], ["承認者メール", "メール", "mail", "email", "approveremail", "approver"])
    assert idx == 1


def test_pick_dept_approver_sheet():
    assert sync.pick_dept_approver_sheet(["x", "DeptApprovers", "ApproverMap"]) == "DeptApprovers"
    assert sync.pick_dept_approver_sheet(["x", "ApproverMap"]) == "ApproverMap"
    assert sync.pick_dept_approver_sheet(["x"]) is None


def test_sync_dept_supervisors_keeps_db_when_nothing_valid():
    api = FakeSheetsApi({"ApproverMap": [["部署", "メール"], ["機械部", "a@x"]]})
    result = sync.sync_dept_supervisors(api)
    assert result.ok and result.count == 1 and result.rows[0]["email"] == "a@x"
    only_header = sync.sync_dept_supervisors(FakeSheetsApi({"DeptApprovers": [["部署", "メール"]]}))
    assert only_header.skipped and only_header.rows is None
    no_valid = sync.sync_dept_supervisors(FakeSheetsApi({"DeptApprovers": [["部署", "メール"], ["", "a@x"]]}))
    assert no_valid.skipped and no_valid.rows is None
