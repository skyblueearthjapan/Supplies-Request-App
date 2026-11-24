import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Integer, MetaData, Numeric, Table, Text, create_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "tools"))

from app.services import sheet_codec as codec  # noqa: E402
from app.services.sheets import decode_table  # noqa: E402
import migrate_from_sheets as mig  # noqa: E402


def make_schema(engine):
    """担当 F の models の代わり（列名は sheet_codec の snake_case と同じ）。"""
    md = MetaData()
    types = {"datetime": DateTime(timezone=True), "date": Date, "number": Numeric(18, 4), "int": Integer,
             "bool_false": Boolean, "bool_true": Boolean, "route": JSON}
    for table, spec in codec.TABLES.items():
        cols = [Column("id", Integer, primary_key=True, autoincrement=True)]
        for column in spec.columns:
            cols.append(Column(codec.db_key(column), types.get(spec.kind(column), Text)))
        if spec.order_key:
            cols.append(Column(spec.order_key, Integer))
        Table(table, md, *cols)
    md.create_all(engine)
    return md


def sheet_values():
    req = [
        ["REQ-20261124093000-000001", "2026-11-24 09:30:00", "2026-11-24 09:30:00", "2026-11-24 09:30:00", "",
         "a@x", "山田", "機械部", "2026-11-24", "A", "3/8", 1000, "承認中", "上席", "b@x", "上席",
         '["上席","総務部長","購買手配"]', "", "", "1.0.0", "", "", ""],
        ["REQ-20261124100000-000002", "2026-11-24 10:00:00", "2026-11-24 11:00:00", "2026-11-24 10:00:00",
         "2026-11-24 11:00:00", "a@x", "山田", "機械部", "2026-11-24", "B", "", 250000, "完了", "完了済", "", "",
         '["上席","総務部長","社長","購買手配"]', "f1", "u1", "1.0.0", "", "ELEC", ""],
        ["SAMPLE-0001", "2026-06-01", "2026-06-01", "2026-06-01", "", "s@x", "見本", "機械部", "2026-06-01", "A",
         "", 5, "承認中", "上席", "b@x", "上席", '["上席"]', "", "", "1.0.0", "", "メカ", ""],
        ["TEST-EXP-0001", "2026-06-01", "2026-06-01", "2026-06-01", "", "s@x", "試験", "機械部", "2026-06-01", "A",
         "", 7, "取消", "申請者", "", "", '["上席"]', "", "", "1.0.0", "true", "メカ", ""],
    ]
    items = [
        ["ITEM-1", "REQ-20261124093000-000001", 1, "配管", "0123", "=1+1", 2, 500, 1000, "", "1/2", "個"],
        ["ITEM-2", "REQ-20261124100000-000002", 1, "工具", 123.0, "", 1, 250000, 250000, "", "", ""],
        ["ITEM-3", "SAMPLE-0001", 1, "見本", "", "", 1, 5, 5, "", "", ""],
    ]
    history = [
        ["HIS-1", "REQ-20261124093000-000001", "2026-11-24 09:30:00", "a@x", "山田", "申請", "", "承認中", "", "上席", ""],
        ["HIS-2", "SAMPLE-0001", "2026-06-01 00:00:00", "s@x", "見本", "申請", "", "承認中", "", "上席", ""],
    ]
    return {
        "requests": [codec.REQUEST_HEADERS] + req,
        "request_items": [codec.ITEM_HEADERS] + items,
        "approver_rules": [codec.APPROVER_HEADERS, ["機械部"] + [""] * 10 + [""] + [""] * 7],
        "history": [codec.HISTORY_HEADERS] + history,
        "settings": [codec.SETTING_HEADERS, ["thresholdAmount", 100000, "説明"], ["enableEmailNotifications", True, ""]],
        "workers": [codec.WORKER_CACHE_HEADERS, [1, "山田", "機械部", "本社", "社員"]],
        "notification_recipients": [codec.RECIPIENT_HEADERS, ["購買", "p@x", "購買", "", "TO"]],
        "dept_supervisors": [codec.DEPT_SUPERVISOR_HEADERS, ["機械部", "b@x", "上席", "true", "課長", ""]],
    }


def reads_of(values):
    return {t: decode_table(t, v) for t, v in values.items()}


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    make_schema(engine)
    return engine


def test_migrate_into_empty_db_and_report(engine, tmp_path):
    snapshot = tmp_path / "route.json"
    report = mig.run(reads_of(sheet_values()), engine, dry_run=False, snapshot_out=str(snapshot))
    assert report["excludedRequestIds"] == ["SAMPLE-0001", "TEST-EXP-0001"]
    assert report["excludedCounts"] == {"requests": 2, "request_items": 1, "history": 1}
    s = report["summary"]
    assert s["counts"]["requests"] == 2 and s["counts"]["request_items"] == 2 and s["counts"]["history"] == 1
    assert s["totalAmount"] == "251000"
    assert s["statusCounts"] == {"完了": 1, "承認中": 1}
    assert s["inReviewStepCounts"] == {"上席": 1}
    assert report["verifyProblems"] == []
    assert report["droppedColumns"] == {}

    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert data[0] == {
        "requestId": "REQ-20261124093000-000001", "status": "承認中", "currentStep": "上席",
        "currentApproverEmail": "b@x", "currentApproverName": "上席", "route": ["上席", "総務部長", "購買手配"],
        "department": "機械部", "applicantEmail": "a@x", "category": "一般不明", "totalAmount": "1000",
        "amountWaived": False,
    }
    assert data[1]["category"] == "電気"

    with engine.connect() as conn:
        row = conn.exec_driver_sql("select model, maker, note from request_items where item_id='ITEM-1'").one()
        assert tuple(row) == ("0123", "=1+1", "1/2")
        assert conn.exec_driver_sql("select model from request_items where item_id='ITEM-2'").scalar() == "123"
        assert conn.exec_driver_sql("select value from settings where key='enableEmailNotifications'").scalar() == "true"


def test_migrate_stops_when_db_not_empty(engine):
    mig.run(reads_of(sheet_values()), engine, dry_run=False)
    with pytest.raises(mig.MigrationStop, match="空ではありません"):
        mig.run(reads_of(sheet_values()), engine, dry_run=False)


def test_migrate_stops_on_duplicate_request_id(engine):
    values = sheet_values()
    values["requests"].append(list(values["requests"][1]))
    with pytest.raises(mig.MigrationStop, match="重なって"):
        mig.run(reads_of(values), engine, dry_run=False)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from requests").scalar() == 0


def test_migrate_stops_on_unknown_enum():
    values = sheet_values()
    values["requests"][1][12] = "IN_REVIEW"
    with pytest.raises(codec.CodecError):
        reads_of(values)


def test_dry_run_does_not_need_db():
    report = mig.run(reads_of(sheet_values()), None, dry_run=True)
    assert report["dryRun"] is True and "verifyProblems" not in report


def test_source_must_be_production_id():
    with pytest.raises(mig.MigrationStop):
        mig.read_from_sheets("other-id", lambda: "t")
    with pytest.raises(mig.MigrationStop):
        mig.read_from_sheets(codec.LEGACY_SPREADSHEET_ID, lambda: "t", allow_non_production=True)


def test_main_requires_database_url(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert mig.main(["--source", "csv", "--dir", str(tmp_path)]) == 2
    assert "未設定" in capsys.readouterr().err


def test_csv_and_xlsx_sources(tmp_path, engine):
    import openpyxl

    values = sheet_values()
    for table, rows in values.items():
        with (tmp_path / f"{codec.TABLES[table].sheet}.csv").open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
    report = mig.run(mig.read_from_csv_dir(str(tmp_path)), None, dry_run=True)
    assert report["summary"]["totalAmount"] == "251000"

    book = openpyxl.Workbook()
    book.remove(book.active)
    for table, rows in values.items():
        sheet = book.create_sheet(codec.TABLES[table].sheet)
        for row in rows:
            sheet.append(row)
    # 日付のセル（xlsx では datetime）
    book["申請"]["B2"] = datetime(2026, 11, 24, 0, 30)
    path = tmp_path / "data.xlsx"
    book.save(path)
    assert mig.main(["--source", "xlsx", "--file", str(path), "--database-url", str(engine.url),
                     "--report-out", str(tmp_path / "r.json")]) == 0
    report = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert report["verifyProblems"] == []
    with engine.connect() as conn:
        created = conn.exec_driver_sql(
            "select created_at from requests where request_id='REQ-20261124093000-000001'").scalar()
    assert str(created).startswith("2026-11-24 00:30:00")


def test_db_value_turns_lists_into_json_not_pg_array():
    # 承認経路を list のまま入れると PostgreSQL の配列表記になり、アプリが読めなくなった不具合の再発防止
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "tools" / "migrate_from_sheets.py"
    spec = importlib.util.spec_from_file_location("migrate_tool_for_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._db_value(["上席", "購買見積"]) == '["上席","購買見積"]'
    assert mod._db_value("文字") == "文字"
    assert mod._db_value(None) is None
