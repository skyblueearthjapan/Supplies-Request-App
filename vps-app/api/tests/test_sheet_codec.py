import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import sheet_codec as codec  # noqa: E402

UTC = timezone.utc


def serial_of(naive: datetime) -> float:
    """シートの時計（naive）→ シリアル値。"""
    return (naive - datetime(1899, 12, 30)).total_seconds() / 86400


def gas_request_row(**overrides):
    """GAS 版の createRequest が appendObject_ に渡す値（toCellValue_ の ' を除いたもの）。"""
    row = {
        "requestId": "REQ-20261124093000-000123",
        "createdAt": "2026-11-24 09:30:00",
        "updatedAt": "2026-11-24 10:15:42",
        "submittedAt": "2026-11-24 09:30:00",
        "completedAt": "",
        "applicantEmail": "taro@lineworks-local.info",
        "applicantName": "山田 太郎",
        "department": "機械部",
        "requestDate": "2026-11-24",
        "reasonCode": "A",
        "reasonDetail": "=1+1 のため 3/8 を 1/2 個",
        "totalAmount": 12345,
        "status": "承認中",
        "currentStep": "上席",
        "currentApproverEmail": "boss@lineworks-local.info",
        "currentApproverName": "上席 一郎",
        "routeJson": '["上席","総務部長","購買手配"]',
        "pdfFileId": "",
        "pdfUrl": "",
        "version": "1.0.0",
        "amountWaived": "true",
        "category": "メカ",
        "splitFromRequestId": "",
    }
    row.update(overrides)
    return [row[c] for c in codec.REQUEST_COLUMNS]


def roundtrip(table, row, formatted=None, tz=codec.JST):
    ctx = codec.DecodeContext(sheet_tz=tz)
    record = codec.sheet_row_to_db(table, row, formatted, 2, ctx)
    return record, codec.db_to_sheet_row(table, record), ctx


def test_request_roundtrip_matches_gas_values():
    row = gas_request_row()
    record, back, _ = roundtrip("requests", row)
    assert back == row
    assert record["request_id"] == "REQ-20261124093000-000123"
    assert record["created_at"] == datetime(2026, 11, 24, 9, 30, tzinfo=codec.JST)
    assert record["request_date"] == date(2026, 11, 24)
    assert record["total_amount"] == Decimal(12345)
    assert record["route_json"] == ["上席", "総務部長", "購買手配"]
    assert record["amount_waived"] is True


def test_item_roundtrip_keeps_free_text():
    row = ["ITEM-1", "REQ-1", 1, "3/8", "0123", "=1+1", 1.5, 1200, 1800, "2026-12-01", "1/2", "ケース"]
    record, back, _ = roundtrip("request_items", row)
    assert back == row
    assert record["name"] == "3/8"
    assert record["model"] == "0123"
    assert record["maker"] == "=1+1"
    assert record["note"] == "1/2"
    assert record["quantity"] == Decimal("1.5")


BASE_ROWS = {
    "requests": gas_request_row(),
    "request_items": ["ITEM-1", "REQ-1", 1, "", "", "", 1, 1, 1, "", "", ""],
    "approver_rules": ["機械部"] + [""] * 18,
    "history": ["HIS-1", "REQ-1", "2026-11-24 09:00:00", "a@x", "", "申請", "", "", "", "", ""],
    "settings": ["k", "", ""],
    "workers": ["1", "", "", "", ""],
    "notification_recipients": ["購買", "p@x", "", "", "TO"],
    "dept_supervisors": ["機械部", "b@x", "", "", "", ""],
}


@pytest.mark.parametrize("text", ["3/8", "0123", "=1+1", "1/2", "'quoted", "+81-3"])
def test_free_text_stays_string_in_every_free_column(text):
    checked = 0
    for table, spec in codec.TABLES.items():
        for column in spec.columns:
            if column not in codec.FREE_TEXT_COLUMNS:
                continue
            row = list(BASE_ROWS[table])
            row[spec.columns.index(column)] = text
            record, back, _ = roundtrip(table, row)
            assert record[codec.db_key(column)] == text
            assert back[spec.columns.index(column)] == text
            checked += 1
    assert checked >= 15


def test_numbers_in_text_columns_become_strings_without_dot_zero():
    record, back, _ = roundtrip("workers", [123.0, "山田", "機械部", "本社", "社員"])
    assert record["worker_code"] == "123"
    record, _, _ = roundtrip("request_items", ["ITEM-1", "REQ-1", 1, "部品", 4567.0, 12, 1, 100, 100, "", "", ""])
    assert record["model"] == "4567"
    assert record["maker"] == "12"


def test_datetime_serial_across_jst_midnight():
    # シートのタイムゾーンが Asia/Tokyo: 日本時間 2026-11-24 00:30 のセル
    s = serial_of(datetime(2026, 11, 24, 0, 30))
    record, back, _ = roundtrip("requests", gas_request_row(createdAt=s, requestDate=serial_of(datetime(2026, 11, 24))))
    assert record["created_at"] == datetime(2026, 11, 24, 0, 30, tzinfo=codec.JST)
    assert back[1] == "2026-11-24 00:30:00"
    assert back[8] == "2026-11-24"

    # シートのタイムゾーンが UTC: UTC 2026-11-23 15:30 = 日本時間 2026-11-24 00:30
    s = serial_of(datetime(2026, 11, 23, 15, 30))
    d = serial_of(datetime(2026, 11, 23, 15, 0))  # 日本時間の 11/24 0 時
    record, back, _ = roundtrip("requests", gas_request_row(createdAt=s, requestDate=d), tz=UTC)
    assert record["created_at"].astimezone(codec.JST).date() == date(2026, 11, 24)
    assert back[1] == "2026-11-24 00:30:00"
    assert record["request_date"] == date(2026, 11, 24)


def test_db_datetime_in_utc_is_written_in_jst():
    record = codec.sheet_row_to_db("requests", gas_request_row(), None, 2)
    record["updated_at"] = datetime(2026, 11, 23, 15, 0, 1, tzinfo=UTC)
    record["completed_at"] = datetime(2026, 11, 23, 23, 59, 59)  # 時差なし = 日本時間の時計
    row = codec.db_to_sheet_row("requests", record)
    assert row[2] == "2026-11-24 00:00:01"
    assert row[4] == "2026-11-23 23:59:59"


def test_old_row_without_time_becomes_midnight():
    record, back, _ = roundtrip("requests", gas_request_row(createdAt="2026-06-01"))
    assert record["created_at"] == datetime(2026, 6, 1, 0, 0, tzinfo=codec.JST)
    assert back[1] == "2026-06-01 00:00:00"


def test_us_formatted_datetime_text_is_read():
    record, _, _ = roundtrip("requests", gas_request_row(updatedAt="11/24/2026 9:05:07"))
    assert record["updated_at"] == datetime(2026, 11, 24, 9, 5, 7, tzinfo=codec.JST)


def test_free_text_turned_into_date_is_reported():
    s = serial_of(datetime(2026, 3, 8))
    row = ["ITEM-1", "REQ-1", 1, "部品", s, "", 1, 100, 100, "", "", ""]
    formatted = ["ITEM-1", "REQ-1", "1", "部品", "3/8/2026", "", "1", "100", "100", "", "", ""]
    record, _, ctx = roundtrip("request_items", row, formatted)
    assert record["model"] == "2026-03-08"  # GAS 版の normalizeCellValue_ と同じ値
    assert any("型番" in w or "model" in w for w in ctx.warnings)


def test_desired_delivery_date_cell_becomes_jst_date_text():
    s = serial_of(datetime(2026, 12, 1))
    record, back, ctx = roundtrip("request_items", ["ITEM-1", "REQ-1", 1, "部品", "", "", 1, 1, 1, s, "", ""])
    assert record["desired_delivery_date"] == "2026-12-01"
    assert back[9] == "2026-12-01"
    assert ctx.warnings == []


@pytest.mark.parametrize("raw, expected", [
    ("true", True), (True, True), ("TRUE", True), ("", False), (False, False), ("false", False), ("x", False),
])
def test_amount_waived_blank_is_false(raw, expected):
    record, back, _ = roundtrip("requests", gas_request_row(amountWaived=raw))
    assert record["amount_waived"] is expected
    assert back[codec.REQUEST_COLUMNS.index("amountWaived")] == ("true" if expected else "false")


@pytest.mark.parametrize("raw, expected", [
    ("", True), ("true", True), (True, True), ("false", False), (False, False), ("FALSE", False),
])
def test_active_blank_is_true(raw, expected):
    for table, row in (
        ("approver_rules", ["機械部"] + [""] * 10 + [raw] + [""] * 7),
        ("notification_recipients", ["購買", "p@x", "購買", raw, "TO"]),
        ("dept_supervisors", ["機械部", "b@x", "上席", raw, "", ""]),
    ):
        record, back, _ = roundtrip(table, row)
        assert record["active"] is expected
        assert back[codec.TABLES[table].columns.index("active")] == ("true" if expected else "false")


@pytest.mark.parametrize("raw, expected", [
    ("", "一般不明"), ("MECH", "メカ"), ("elec", "電気"), ("GENERAL", "一般不明"), ("電気", "電気"), ("メカ", "メカ"),
])
def test_category_is_normalized(raw, expected):
    record, back, _ = roundtrip("requests", gas_request_row(category=raw))
    assert record["category"] == expected
    assert back[codec.REQUEST_COLUMNS.index("category")] == expected


@pytest.mark.parametrize("column, value", [
    ("status", "IN_REVIEW"), ("status", ""), ("currentStep", "SUPERVISOR"), ("category", "OTHER"),
    ("routeJson", '["上席","PRESIDENT"]'), ("routeJson", "not json"), ("reasonCode", "Z"),
    ("totalAmount", "abc"), ("createdAt", "昨日"),
])
def test_unknown_values_raise(column, value):
    with pytest.raises(codec.CodecError):
        codec.sheet_row_to_db("requests", gas_request_row(**{column: value}), None, 5)


def test_history_enums():
    row = ["HIS-1", "REQ-1", "2026-11-24 09:00:00", "a@x", "山田", "申請", "", "承認中", "", "上席", "3/8"]
    record, back, _ = roundtrip("history", row)
    assert back == row
    assert record["seq"] == 1
    with pytest.raises(codec.CodecError):
        codec.sheet_row_to_db("history", row[:5] + ["SUBMIT"] + row[6:], None, 2)
    with pytest.raises(codec.CodecError):
        codec.sheet_row_to_db("history", row[:8] + ["UNKNOWN"] + row[9:], None, 2)


def test_recipient_type_and_send_as():
    record, back, _ = roundtrip("notification_recipients", ["電気購買", "e@x", "電気", "", "cc"])
    assert record["send_as"] == "CC" and back[4] == "CC"
    record, back, _ = roundtrip("notification_recipients", ["総務部", "g@x", "", "", ""])
    assert record["send_as"] == "TO" and back[4] == "TO"
    with pytest.raises(codec.CodecError):
        codec.sheet_row_to_db("notification_recipients", ["PURCHASING", "p@x", "", "", ""], None, 2)


def test_settings_values():
    record, back, _ = roundtrip("settings", ["enableEmailNotifications", False, "説明"])
    assert record["value"] == "false" and back[1] == "false"
    record, back, _ = roundtrip("settings", ["thresholdAmount", 100000, "説明"])
    assert record["value"] == "100000" and back[1] == "100000"
    record, back, _ = roundtrip("settings", ["adminEmails", "a@x, b@x", ""])
    assert back == ["adminEmails", "a@x, b@x", ""]


def test_version_and_route_are_written_in_gas_form():
    record = codec.sheet_row_to_db("requests", gas_request_row(version=""), None, 2)
    record["route_json"] = ["上席", "購買見積", "総務部長"]
    row = codec.db_to_sheet_row("requests", record)
    assert row[codec.REQUEST_COLUMNS.index("version")] == "1.0.0"
    assert row[codec.REQUEST_COLUMNS.index("routeJson")] == '["上席","購買見積","総務部長"]'


def test_master_order_is_kept():
    rows = [codec.sheet_row_to_db("dept_supervisors", ["機械部", f"{n}@x", "", "", "", ""], None, n + 2)
            for n in range(3)]
    shuffled = [rows[2], rows[0], rows[1]]
    values = codec.db_rows_to_sheet_values("dept_supervisors", shuffled)
    assert [v[1] for v in values] == ["0@x", "1@x", "2@x"]


def test_xlsx_datetime_objects_are_read():
    record, back, _ = roundtrip("requests", gas_request_row(createdAt=datetime(2026, 11, 24, 0, 5),
                                                           requestDate=datetime(2026, 11, 24)))
    assert back[1] == "2026-11-24 00:05:00"
    assert record["request_date"] == date(2026, 11, 24)


def test_js_string():
    assert codec.js_string(123.0) == "123"
    assert codec.js_string(1.5) == "1.5"
    assert codec.js_string(True) == "true"
    assert codec.js_string(None) == ""
    assert codec.js_string(Decimal("2.50")) == "2.5"
    assert codec.js_string(Decimal("100")) == "100"
    assert codec.db_key("pdfUrl") == "pdf_url"
    assert codec.db_key("purchasingElecEmail") == "purchasing_elec_email"
    assert codec.db_key("notifyEmail") == "notify_email"
