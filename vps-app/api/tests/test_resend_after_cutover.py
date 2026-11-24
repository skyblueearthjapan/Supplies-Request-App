"""切り替えの直後の送り直し（tools/resend_after_cutover.py）。SQLite で、積む仕事・宛先・二重実行の止め方を確かめる。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.constants import STATUS, STEPS
from app.models import Job, NotificationRecipient, Request, SyncRun
from tests.test_domain_workflow import (  # noqa: F401 - フィクスチャを使う
    APP1, ELEC, GM, MECH, PRES, SUP1, SUP2, call, create, handlers, seeded, to_gm, to_quote,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import resend_after_cutover as resend  # noqa: E402


def _set_step(db, rid, step, email="", name=""):
    r = db.get(Request, rid)
    r.current_step, r.current_approver_email, r.current_approver_name = step, email, name
    db.commit()


@pytest.fixture
def cases(call, db):
    """承認中の各段と、差戻し・完了の申請を 1 件ずつ作り、作ったときの仕事は消しておく。"""
    db.add(NotificationRecipient(sort_order=2, type="購買", email=MECH, name="購買", active=True, send_as="TO"))
    db.add(NotificationRecipient(sort_order=3, type="電気購買", email=ELEC, name="電気購買", active=True, send_as="TO"))
    db.commit()
    ids = {}
    ids["sup"] = create(call)
    ids["quote"] = create(call)
    to_quote(call, ids["quote"])
    ids["gm"] = create(call)
    to_gm(call, ids["gm"])
    ids["pres"] = create(call)
    to_gm(call, ids["pres"])
    _set_step(db, ids["pres"], STEPS.PRESIDENT, PRES, "社長 太郎")
    ids["arrange"] = create(call)
    to_gm(call, ids["arrange"])
    _set_step(db, ids["arrange"], STEPS.PURCHASING, MECH, "五十嵐（購買）")
    ids["returned"] = create(call)
    call.ok(SUP1, "returnRequest", ids["returned"], "直してください", False)
    ids["done"] = create(call)
    r = db.get(Request, ids["done"])
    r.status, r.current_step = STATUS.COMPLETED, STEPS.DONE
    db.execute(delete(Job))
    db.commit()
    return ids


def _mails_by_request(plans):
    return {p.request_id: p for p in plans}


def test_dry_run_lists_recipients_and_writes_nothing(cases, session_factory, settings, db):
    text, count = resend.run(session_factory, settings, dry_run=True)
    assert count == 0
    assert db.scalars(select(Job)).all() == []
    assert db.scalars(select(SyncRun)).all() == []
    assert "dry-run" in text
    assert "すべて imaizumi@lineworks-local.info へ転送されます" in text  # staging
    assert f"{cases['returned']}" not in text and f"{cases['done']}" not in text
    assert f"[貯蔵品購入申請] 見積依頼 {cases['quote']}" in text
    assert f"[貯蔵品購入申請] 手配依頼 {cases['arrange']}" in text
    assert "申請 5 件" in text


def test_plans_match_gas_recipients(cases, session_factory, settings):
    config = resend.MailPdfConfig.from_settings(settings)
    plans = _mails_by_request(resend.plan_resends(session_factory, config, {"email": "x", "name": "y"}))
    assert set(plans) == {cases[k] for k in ("sup", "quote", "gm", "pres", "arrange")}  # 差戻し・完了は送らない

    sup = plans[cases["sup"]]
    assert sup.kind == "dispatchApprovalNotifications_"
    assert sorted(m.to[0] for m in sup.mails) == sorted([SUP1, SUP2])  # 上席マスタの全員
    assert all(m.subject == f"[貯蔵品購入申請] 承認依頼 {cases['sup']}" for m in sup.mails)

    gm = plans[cases["gm"]]
    assert gm.kind == "sendApprovalRequestEmail_"
    assert [m.to for m in gm.mails] == [[GM]]  # 総務部長は代表 1 人だけ

    pres = plans[cases["pres"]]
    assert [m.to for m in pres.mails] == [[PRES]]

    for key, subject in (("quote", "見積依頼"), ("arrange", "手配依頼")):
        p = plans[cases[key]]
        assert p.kind == "dispatchApprovalNotifications_" and p.args[2] in (STEPS.PURCHASING_QUOTE, STEPS.PURCHASING)
        assert len(p.mails) == 1 and p.mails[0].attach_pdf  # 新しい PDF を添付
        assert p.mails[0].subject == f"[貯蔵品購入申請] {subject} {cases[key]}"
        assert p.mails[0].to == [MECH] and p.mails[0].cc == [ELEC]  # メカは購買が To、電気購買が Cc


def test_run_enqueues_guarded_jobs_once(cases, session_factory, settings, db):
    text, count = resend.run(session_factory, settings, dry_run=False)
    assert count == 5 and "積みました: 5 件" in text
    jobs = {j.request_id: j for j in db.scalars(select(Job)).all()}
    assert set(jobs) == {cases[k] for k in ("sup", "quote", "gm", "pres", "arrange")}
    quote = jobs[cases["quote"]]
    assert quote.kind == "dispatchApprovalNotifications_"
    assert (quote.guard_status, quote.guard_step) == (STATUS.IN_REVIEW, STEPS.PURCHASING_QUOTE)
    args = json.loads(quote.args_json)
    assert args[0]["requestId"] == cases["quote"] and args[2] == STEPS.PURCHASING_QUOTE
    assert args[4] == {"email": settings.delegate_email, "name": resend.ACTOR_NAME}
    runs = db.scalars(select(SyncRun).where(SyncRun.kind == resend.SYNC_KIND)).all()
    assert len(runs) == 1 and runs[0].ok and runs[0].row_count == 5

    with pytest.raises(resend.ResendStop):
        resend.run(session_factory, settings, dry_run=False)
    db.expire_all()
    assert len(db.scalars(select(Job)).all()) == 5  # 二度目は積まない
    text, _ = resend.run(session_factory, settings, dry_run=True)  # 一覧は出せる（実行済みと注意する）
    assert "実行済み" in text


def test_request_moved_after_planning_is_not_enqueued(cases, session_factory, settings, db):
    config = resend.MailPdfConfig.from_settings(settings)
    plans = resend.plan_resends(session_factory, config, {"email": "x", "name": "y"})
    _set_step(db, cases["sup"], STEPS.PURCHASING_QUOTE, MECH, "五十嵐（購買）")
    assert resend.enqueue_plans(session_factory, plans) == 4
    assert cases["sup"] not in {j.request_id for j in db.scalars(select(Job)).all()}


def test_main_refuses_second_run(cases, session_factory, settings, monkeypatch, capsys):
    import app.config
    import app.db

    monkeypatch.setattr(app.db, "get_sessionmaker", lambda: session_factory)
    monkeypatch.setattr(app.config, "get_settings", lambda: settings)
    assert resend.main([]) == 0
    assert resend.main([]) == 2
    assert "止めました" in capsys.readouterr().err
