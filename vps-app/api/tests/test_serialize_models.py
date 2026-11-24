from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import serialize
from app.models import History, Request, RequestItem
from app.timeutil import new_id


def test_fmt_datetime_is_jst_without_t_or_offset():
    utc = datetime(2026, 11, 24, 15, 30, 5, tzinfo=timezone.utc)
    assert serialize.fmt_datetime(utc) == "2026-11-25 00:30:05"
    assert serialize.fmt_datetime(datetime(2026, 11, 24, 15, 30, 5)) == "2026-11-25 00:30:05"  # 時差なし=UTC
    assert serialize.fmt_datetime(None) == ""


def test_fmt_date_and_numbers():
    assert serialize.fmt_date(date(2026, 1, 2)) == "2026-01-02"
    assert serialize.fmt_date(datetime(2026, 1, 1, 16, 0, tzinfo=timezone.utc)) == "2026-01-02"
    assert serialize.fmt_date(None) == ""
    assert serialize.fmt_number(Decimal("1200.0000")) == 1200
    assert isinstance(serialize.fmt_number(Decimal("1200.0000")), int)
    assert serialize.fmt_number(Decimal("2.5000")) == 2.5
    assert serialize.fmt_number(None) == ""


def test_parse_round_trip_and_missing_time():
    dt = serialize.parse_datetime_jst("2026-11-25 00:30:05")
    assert dt == datetime(2026, 11, 24, 15, 30, 5, tzinfo=timezone.utc)
    assert serialize.fmt_datetime(dt) == "2026-11-25 00:30:05"
    # 時刻が欠けた古い行は日本時間の 0 時
    assert serialize.fmt_datetime(serialize.parse_datetime_jst("2026-07-01")) == "2026-07-01 00:00:00"
    assert serialize.parse_datetime_jst("") is None
    with pytest.raises(ValueError):
        serialize.parse_datetime_jst("2026/07/01")
    assert serialize.parse_date("2026-07-01") == date(2026, 7, 1)
    assert serialize.parse_date("") is None


def test_new_id_shape():
    rid = new_id("REQ", datetime(2026, 11, 24, 15, 0, 0, tzinfo=timezone.utc))
    prefix, stamp, rand = rid.split("-")
    assert prefix == "REQ" and stamp == "20261125000000" and len(rand) == 6 and rand.isdigit()


def _request(**kw):
    base = dict(
        request_id="REQ-20261125000000-000001",
        created_at=datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc),
        applicant_email="a@lineworks-local.info",
        applicant_name="a",
        department="製造部",
        request_date=date(2026, 11, 25),
        reason_code="A",
        total_amount=Decimal("1500"),
        status="承認中",
        current_step="上席",
        category="メカ",
    )
    base.update(kw)
    return Request(**base)


def test_request_round_trip_through_db(db):
    db.add(_request())
    db.add(
        RequestItem(
            item_id="ITEM-1", request_id="REQ-20261125000000-000001", line_no=1, name="ウエス",
            quantity=Decimal("1.5"), unit_price=Decimal("1000"), amount=Decimal("1500"), unit="箱",
        )
    )
    db.commit()
    db.expunge_all()
    r = db.get(Request, "REQ-20261125000000-000001")
    row = serialize.request_row(r)
    assert row["createdAt"] == "2026-11-25 00:00:00"
    assert row["submittedAt"] == "" and row["completedAt"] == ""
    assert row["requestDate"] == "2026-11-25"
    assert row["totalAmount"] == 1500
    assert row["amountWaived"] is False
    assert row["routeJson"] == "[]"
    assert row["version"] == "1.0.0"
    assert r.row_version == 1
    item = serialize.item_row(db.get(RequestItem, "ITEM-1"))
    assert item["quantity"] == 1.5 and item["unitPrice"] == 1000 and item["desiredDeliveryDate"] == ""


@pytest.mark.parametrize("field,value", [("status", "DRAFT"), ("current_step", "SUPERVISOR"), ("category", "MECH")])
def test_request_enum_check_constraints(db, field, value):
    db.add(_request(**{field: value}))
    with pytest.raises(IntegrityError):
        db.commit()


def test_history_accepts_legacy_escalate_and_orders_by_seq(db):
    at = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    for i, action in enumerate(["申請", "社長決裁へ", "承認"]):
        db.add(History(history_id=f"HIS-{i}", request_id="R", happened_at=at, action=action))
    db.commit()
    rows = db.execute(select(History).order_by(History.happened_at, History.seq)).scalars().all()
    assert [h.action for h in rows] == ["申請", "社長決裁へ", "承認"]
    assert serialize.history_row(rows[0])["happenedAt"] == "2026-11-25 00:00:00"


def test_history_rejects_unknown_action(db):
    db.add(History(history_id="HIS-x", request_id="R", action="APPROVE"))
    with pytest.raises(IntegrityError):
        db.commit()
