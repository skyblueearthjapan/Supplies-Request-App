"""jobs: 照合・PDF→メールの段分け・未送信分だけの送り直し・管理者通知・同じ申請は同時に 1 本。

models.Job / MailLog と DbRequestGateway を SQLite（conftest の session_factory）で動かす。
ドライブ・Gmail・Chromium だけ偽物にする。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from mailpdf_fixtures import ORIGIN, approver_rules, dept_supervisors, make_items, make_request, recipients_rows

from app import jobs, models
from app.jobs import MAX_ATTEMPTS, DbRequestGateway, JobRunner, JobStore
from app.services.drive import DriveFile
from app.services.mailpdf_common import MailPdfConfig

T0 = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)
RID = "REQ-20261124093000-123456"
SNAKE = {
    "applicantEmail": "applicant_email", "applicantName": "applicant_name", "supervisorEmail": "supervisor_email",
    "supervisorName": "supervisor_name", "supervisorTitle": "supervisor_title",
    "generalManagerEmail": "general_manager_email", "generalManagerName": "general_manager_name",
    "generalManagerTitle": "general_manager_title", "presidentEmail": "president_email", "presidentName": "president_name",
    "presidentTitle": "president_title", "purchasingEmail": "purchasing_email", "purchasingName": "purchasing_name",
    "purchasingTitle": "purchasing_title", "purchasingElecEmail": "purchasing_elec_email",
    "purchasingElecName": "purchasing_elec_name", "purchasingElecTitle": "purchasing_elec_title",
    "notifyEmail": "notify_email", "sendAs": "send_as",
}


def _active(v):
    return not (v is False or str(v).lower() == "false")


def seed_masters(db):
    for i, r in enumerate(approver_rules()):
        db.add(models.ApproverRule(sort_order=i, active=_active(r.pop("active")), **{SNAKE.get(k, k): v for k, v in r.items()}))
    for i, r in enumerate(dept_supervisors()):
        db.add(models.DeptSupervisor(sort_order=i, active=_active(r.pop("active")), **{SNAKE.get(k, k): v for k, v in r.items()}))
    for i, r in enumerate(recipients_rows()):
        if r["email"]:
            db.add(models.NotificationRecipient(sort_order=i, active=_active(r.pop("active")),
                                                **{SNAKE.get(k, k): v for k, v in r.items()}))
    for k, v in {"enableEmailNotifications": "true", "adminEmails": "imaizumi@lineworks-local.info",
                 "pdfFolderId": "prod-folder-id", "thresholdAmount": "100000"}.items():
        db.add(models.Setting(key=k, value=v))


def seed_request(db, **overrides):
    r = make_request(**overrides)
    db.add(models.Request(
        request_id=r["requestId"], applicant_email=r["applicantEmail"], applicant_name=r["applicantName"],
        department=r["department"], request_date=date(2026, 11, 24), reason_code=r["reasonCode"],
        reason_detail=r["reasonDetail"], total_amount=Decimal(str(r["totalAmount"])), status=r["status"],
        current_step=r["currentStep"], current_approver_email=r["currentApproverEmail"],
        current_approver_name=r["currentApproverName"], route_json=r["routeJson"], pdf_file_id=r["pdfFileId"],
        pdf_url=r["pdfUrl"], amount_waived=r["amountWaived"] == "true", category=r["category"],
        created_at=T0, updated_at=T0, submitted_at=T0))
    for it in make_items():
        db.add(models.RequestItem(
            item_id=r["requestId"] + it["itemId"], request_id=r["requestId"], line_no=it["lineNo"], name=it["name"],
            model=it["model"], maker=it["maker"], quantity=Decimal(str(it["quantity"])),
            unit_price=Decimal(str(it["unitPrice"])), amount=Decimal(str(it["amount"])),
            desired_delivery_date=date(2026, 12, 1) if it["desiredDeliveryDate"] else None, note=it["note"], unit=it["unit"]))
    db.add(models.History(history_id="HIS-" + r["requestId"], request_id=r["requestId"], happened_at=T0,
                          actor_email=r["applicantEmail"], actor_name=r["applicantName"], action="申請",
                          to_status="承認中", to_step="上席"))
    db.commit()
    return r


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.fail_to: set[str] = set()  # 本来の To にこの宛先を含むメールは失敗させる
        self.lose_response = 0  # 送れたのに応答が時間切れになる回数
        self.find_calls = []

    def send(self, msg):
        body = msg.get_body(("plain",)).get_content()
        if any(("本来の To : " + addr) in body for addr in self.fail_to):
            raise RuntimeError("gmail down")
        self.sent.append(msg)
        if self.lose_response:
            self.lose_response -= 1
            raise TimeoutError("read timed out")
        return f"gm-{len(self.sent)}"

    def find_sent(self, message_id):
        self.find_calls.append(message_id)
        for i, m in enumerate(self.sent, start=1):
            if m["Message-ID"] == message_id:
                return f"gm-{i}"
        return ""


class FakeStorage:
    def __init__(self):
        self.saved, self.trashed, self.downloads = [], [], 0
        self.on_save = None

    def save(self, settings, request_date, name, data):
        if self.on_save:
            self.on_save()
        fid = f"F{len(self.saved) + 1}"
        self.saved.append((fid, name, settings.get("pdfFolderId")))
        return DriveFile(id=fid, url=f"https://drive.google.com/file/d/{fid}/view")

    def trash(self, settings, file_id):
        self.trashed.append(file_id)
        return True

    def download(self, file_id):
        self.downloads += 1
        return b"%PDF-downloaded"


class FakeRenderer:
    def __init__(self):
        self.count = 0

    def render(self, html):
        self.count += 1
        assert "貯蔵品購入申請書" in html
        return b"%PDF-1.4 fake"


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class Env:
    pass


@pytest.fixture
def env(session_factory):
    e = Env()
    e.sf = session_factory
    with session_factory() as db:
        seed_masters(db)
        db.commit()
        e.req = seed_request(db)
    e.clock = Clock()
    e.gw = DbRequestGateway(session_factory)
    e.runner = JobRunner(store=JobStore(session_factory), gateway=e.gw, transport=FakeTransport(), storage=FakeStorage(),
                         renderer=FakeRenderer(),
                         config=MailPdfConfig(app_env="staging", public_origin=ORIGIN, mail_redirect_to="imaizumi@lineworks-local.info"),
                         extra_admins=["imaizumi@lineworks-local.info"], now=e.clock)
    e.transport, e.storage, e.renderer = e.runner.transport, e.runner.storage, e.runner.renderer

    def enqueue(kind, args, request_id=RID, guard=True):
        with session_factory() as db:
            row = db.get(models.Request, request_id)
            g = {"status": row.status, "currentStep": row.current_step} if guard and row else None
            job = jobs.enqueue(db, kind, args, request_id, g, now=e.clock())
            db.commit()
            return job.id

    def set_request(request_id=RID, **fields):
        with session_factory() as db:
            row = db.get(models.Request, request_id)
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()

    def mails(job_id=None):
        with session_factory() as db:
            q = select(models.MailLog).order_by(models.MailLog.id)
            if job_id is not None:
                q = q.where(models.MailLog.job_id == job_id)
            return db.scalars(q).all()

    def job(job_id):
        with session_factory() as db:
            return db.get(models.Job, job_id)

    e.enqueue, e.set_request, e.mails, e.job = enqueue, set_request, mails, job
    return e


def snapshot(env):
    return env.gw.get_request(RID)


SUP1 = {"email": "sup1@lineworks-local.info", "name": "上田 一郎", "title": "課長", "notifyEmail": "sup1-phone@lineworks-local.info"}
ACTOR = {"email": "sup1@lineworks-local.info", "name": "sup1"}

# ---------------------------------------------------------------------------


def test_enqueue_contract_and_pending_count(env, session_factory):
    jid = env.enqueue("sendApprovalRequestEmail_", [snapshot(env), SUP1])
    with session_factory() as db:
        row = db.get(models.Job, jid)
        assert (row.kind, row.request_id, row.guard_status, row.guard_step, row.state) == (
            "sendApprovalRequestEmail_", RID, "承認中", "上席", "pending")
        assert json.loads(row.args_json)[1] == SUP1
        assert jobs.pending_count(db, RID) == 1
    assert env.runner.run_once() == {"done": 1, "skipped": 0, "retry": 0, "failed": 0}
    (log,) = env.mails(jid)
    assert (log.kind, log.to_addrs, log.cc_addrs, log.sent, log.redirected_to, log.gmail_message_id) == (
        "approval_request", "sup1@lineworks-local.info", "sup1-phone@lineworks-local.info", True,
        "imaizumi@lineworks-local.info", "gm-1")
    assert env.transport.sent[0]["To"] == "imaizumi@lineworks-local.info"  # staging は必ず転送
    with session_factory() as db:
        assert jobs.pending_count(db, RID) == 0
    assert env.runner.run_once()["done"] == 0  # 2 回は動かない


def test_enqueue_rejects_unknown_kind_and_unguarded_kinds_have_empty_guard(env, session_factory):
    with session_factory() as db, pytest.raises(ValueError):
        jobs.enqueue(db, "sendMail_", [], RID)
    jid = env.enqueue("sendReturnedBroadcast_", [snapshot(env), "上席", "c", "a"])
    assert (env.job(jid).guard_status, env.job(jid).guard_step) == ("", "")


def test_guard_skips_when_state_changed(env):
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "上席", "購買見積", {"email": "mech@x"}, ACTOR])
    env.set_request(current_step="購買見積")  # 承認の直後にリコールなどで段が変わった
    assert env.runner.run_once()["skipped"] == 1
    assert env.renderer.count == 0 and env.transport.sent == [] and env.mails(jid) == []
    assert env.job(jid).state == "skipped"


def test_broadcast_is_not_guarded(env):
    req = snapshot(env)
    jid = env.enqueue("sendReturnedBroadcast_", [req, "上席", "直して", "上田 一郎"])
    env.set_request(status="差戻し", current_step="申請者")
    assert env.runner.run_once()["done"] == 1
    (log,) = env.mails(jid)
    assert log.kind == "returned_broadcast" and "1. ウエス／型式: W-100／数量: 3 ケース" in log.body_text


def test_dispatch_to_quote_makes_pdf_records_history_and_attaches(env, session_factory):
    env.set_request(current_step="購買見積", pdf_file_id="OLD", pdf_url="https://old")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "上席", "購買見積", {"email": "mech@x"}, ACTOR])
    assert env.runner.run_once()["done"] == 1
    assert env.renderer.count == 1
    assert env.storage.saved == [("F1", RID + "_申請 太郎_貯蔵品購入申請書.pdf", "prod-folder-id")]
    assert env.storage.trashed == ["OLD"]  # 1 申請 = 1 ファイル
    with session_factory() as db:
        row = db.get(models.Request, RID)
        assert (row.pdf_file_id, row.pdf_url, row.row_version) == ("F1", "https://drive.google.com/file/d/F1/view", 2)
        h = db.scalars(select(models.History).where(models.History.action == "PDF作成")).one()
        assert (h.actor_email, h.from_status, h.from_step, h.to_step, h.comment) == (
            "sup1@lineworks-local.info", "承認中", "購買見積", "購買見積", "https://drive.google.com/file/d/F1/view")
        assert h.history_id.startswith("HIS-")
    (log,) = env.mails(jid)
    assert log.kind == "quote_request" and log.attachment_file_id == "F1"
    att = list(env.transport.sent[0].iter_attachments())
    assert att[0].get_filename() == RID + "_申請 太郎_貯蔵品購入申請書.pdf"
    assert att[0].get_content() == b"%PDF-1.4 fake"


def test_complete_mails_use_row_after_pdf(env):
    env.set_request(status="完了", current_step="完了済", total_amount=Decimal("3600"))
    jid = env.enqueue("completeAndNotify_", [snapshot(env), ACTOR])
    env.runner.run_once()
    logs = env.mails(jid)
    assert [(m.kind, m.attachment_file_id) for m in logs] == [("ga_arranged", "F1"), ("completed", "")]
    assert "PDF: https://drive.google.com/file/d/F1/view" in logs[1].body_text


def test_cancelled_while_making_pdf_discards_pdf_and_mail(env, session_factory):
    env.set_request(current_step="購買手配")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "総務部長", "購買手配", {}, ACTOR])
    env.storage.on_save = lambda: env.set_request(status="取消")  # PDF を作っている間に却下された
    assert env.runner.run_once()["skipped"] == 1
    assert env.storage.trashed == ["F1"]
    assert env.transport.sent == [] and env.mails(jid) == []
    with session_factory() as db:
        assert db.get(models.Request, RID).pdf_file_id == ""


def test_retry_resends_only_unsent_mails_with_backoff_then_notifies_admin(env, session_factory):
    env.set_request(current_step="総務部長")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "購買見積", "総務部長", {"email": "gm1@x"}, ACTOR])
    env.transport.fail_to = {"gm2@lineworks-local.info"}
    assert env.runner.run_once()["retry"] == 1
    assert [(m.to_addrs, m.sent) for m in env.mails(jid)] == [
        ("gm1@lineworks-local.info", True), ("gm2@lineworks-local.info", False)]
    assert len(env.transport.sent) == 1
    env.clock.advance(minutes=4)
    assert env.runner.run_once() == {"done": 0, "skipped": 0, "retry": 0, "failed": 0}  # 5 分たつまで動かない
    env.clock.advance(minutes=1)
    assert env.runner.run_once()["retry"] == 1
    assert len(env.transport.sent) == 1  # gm1 には 2 通目を送らない
    env.clock.advance(minutes=30)
    assert env.runner.run_once()["retry"] == 1
    env.clock.advance(minutes=119)
    assert env.runner.run_once()["retry"] == 0  # 2 時間まだ
    env.clock.advance(minutes=1)
    assert env.runner.run_once()["failed"] == 1
    j = env.job(jid)
    assert j.state == "failed" and j.attempts == MAX_ATTEMPTS == 4 and "gmail down" in j.last_error
    assert [m.attempts for m in env.mails(jid)] == [1, 4]
    admin = [m for m in env.mails() if m.job_id is None]
    assert len(admin) == 1 and admin[0].kind == "admin_job_failure" and admin[0].sent
    assert admin[0].to_addrs == "imaizumi@lineworks-local.info"
    assert "処理: 承認後の通知・PDF（dispatchApprovalNotifications_）" in admin[0].body_text
    with session_factory() as db:
        failed = jobs.list_failed(db)
        assert [(f["jobId"], f["kind"], f["label"], f["requestId"], f["attempts"]) for f in failed] == [
            (jid, "dispatchApprovalNotifications_", "承認後の通知・PDF", RID, 4)]
        assert failed[0]["failedAt"] == "2026-11-24 12:35:00"  # 10:00 JST + 155 分（日本時間で返す）
        # 管理画面から送り直す: 未送信の gm2 だけ送る
        assert jobs.retry_failed(db, jid, now=env.clock()) is True
        assert jobs.retry_failed(db, 99999) is False
        db.commit()
    env.transport.fail_to = set()
    assert env.runner.run_once()["done"] == 1
    assert len(env.transport.sent) == 3  # gm1, 管理者通知, gm2
    assert all(m.sent for m in env.mails(jid))


def test_retry_does_not_rebuild_or_remake_pdf(env):
    env.set_request(current_step="購買手配")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "総務部長", "購買手配", {}, ACTOR])
    env.transport.fail_to = {"mech@lineworks-local.info"}
    assert env.runner.run_once()["retry"] == 1
    env.transport.fail_to = set()
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["done"] == 1
    assert env.renderer.count == 1 and len(env.storage.saved) == 1  # PDF は作り直さない
    assert env.storage.downloads == 1  # 添付はドライブから読み直す
    assert len(env.mails(jid)) == 1 and env.mails(jid)[0].sent


def test_guard_checked_again_on_retry(env):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.fail_to = {"applicant@lineworks-local.info"}
    env.runner.run_once()
    env.set_request(current_step="購買見積")
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["skipped"] == 1
    (m,) = env.mails(jid)
    assert m.sent is False and m.last_error.startswith("skipped:")


def test_same_request_runs_one_at_a_time_in_order(env, session_factory):
    with session_factory() as db:
        seed_request(db, requestId="REQ-OTHER")
    store = JobStore(session_factory)
    a = env.enqueue("sendReturnedEmail_", [snapshot(env), "1"])
    b = env.enqueue("sendRejectedEmail_", [snapshot(env), "2"])
    c = env.enqueue("sendReturnedEmail_", [env.gw.get_request("REQ-OTHER"), "3"], request_id="REQ-OTHER")
    assert [j.id for j in store.claim(env.clock())] == [a, c]  # 同じ申請の 2 本目は後回し
    assert store.claim(env.clock()) == []  # a が動いている間は b を取らない
    store.update_job(a, env.clock(), state="done")
    assert [j.id for j in store.claim(env.clock())] == [b]


def test_stale_running_job_is_reclaimed(env, session_factory):
    store = JobStore(session_factory)
    a = env.enqueue("sendReturnedEmail_", [snapshot(env), "1"])
    assert [j.id for j in store.claim(env.clock())] == [a]
    env.clock.advance(minutes=16)  # worker が止まった
    (j,) = store.claim(env.clock())
    assert j.id == a and j.attempts == 2


def test_split_notification_and_regenerate_pdf(env, session_factory):
    with session_factory() as db:
        seed_request(db, requestId="REQ-NEW", status="差戻し", currentStep="申請者")
    env.set_request(current_step="社長", pdf_file_id="P0")
    j1 = env.enqueue("notifySplitReturned_", ["REQ-NEW", "総務部長", "分けた", "辻野"], guard=False)
    j2 = env.enqueue("regeneratePdfAfterSplit_", [snapshot(env), "社長", ACTOR])
    j3 = env.enqueue("regeneratePdfAfterSplit_", [snapshot(env), "購買手配", ACTOR])
    for _ in range(3):
        env.runner.run_once()
    assert [m.kind for m in env.mails(j1)] == ["split_returned", "split_returned_broadcast"]
    assert env.job(j2).state == "done" and env.job(j3).state == "done"
    assert env.renderer.count == 1 and env.storage.trashed == ["P0"]  # j3 は遷移先で作るので作らない


def test_general_affairs_kind_from_subject(env):
    from app.services.recipients import general_affairs_body, quote_confirmed_texts

    env.set_request(current_step="総務部長", total_amount=Decimal("120000"))
    req = snapshot(env)
    t = quote_confirmed_texts(RID, "¥120,000（税抜）", 120000, 100000, True)
    body = general_affairs_body(env.gw.get_masters(), ORIGIN, req, t["gaHeading"])
    jid = env.enqueue("notifyGeneralAffairs_", [req, t["gaSubject"], body, None])
    j2 = env.enqueue("sendApprovalRequestEmail_", [req, {"email": "gm1@lineworks-local.info"}, t["extraNote"]])
    env.runner.run_once()
    env.runner.run_once()
    (ga,) = env.mails(jid)
    assert ga.kind == "ga_quote_confirmed_over" and ga.subject.startswith("【🚨至急・要社長決裁🚨】")
    assert (ga.to_addrs, ga.cc_addrs) == ("ga1@lineworks-local.info", "ga2@lineworks-local.info")
    assert ga.body_text.startswith("総務 辻野部長\n\n━━")
    (ap,) = env.mails(j2)
    assert ap.kind == "approval_request_over" and ap.subject.startswith("【★至急・要社長決裁★】")


def test_production_redirect_only_while_test_redirect_is_set(env, session_factory):
    env.runner.config = MailPdfConfig(app_env="production", public_origin=ORIGIN)
    env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.runner.run_once()
    assert env.transport.sent[0]["To"] == "applicant@lineworks-local.info"
    with session_factory() as db:
        until = int((env.clock() + timedelta(hours=1)).timestamp() * 1000)
        db.add(models.Setting(key="mailRedirectTo", value=f"imaizumi@lineworks-local.info|{until}"))
        db.commit()
    env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.runner.run_once()
    assert env.transport.sent[1]["To"] == "imaizumi@lineworks-local.info"


# ---------------------------------------------------------------------------
# VPS 版で足した kind（担当 W が積む）: regeneratePdf・trashPdf
# ---------------------------------------------------------------------------


def test_regenerate_pdf_makes_pdf_records_history_and_sends_no_mail(env, session_factory):
    env.set_request(status="完了", current_step="完了済", pdf_file_id="P0", pdf_url="https://old")
    jid = env.enqueue("regeneratePdf", [RID, ACTOR])
    assert (env.job(jid).guard_status, env.job(jid).guard_step) == ("完了", "完了済")
    assert env.runner.run_once() == {"done": 1, "skipped": 0, "retry": 0, "failed": 0}
    assert env.renderer.count == 1 and env.storage.trashed == ["P0"]  # 前の PDF はゴミ箱へ
    assert env.mails(jid) == [] and env.transport.sent == []
    with session_factory() as db:
        row = db.get(models.Request, RID)
        assert (row.pdf_file_id, row.pdf_url) == ("F1", "https://drive.google.com/file/d/F1/view")
        hist = db.scalars(select(models.History).where(models.History.action == "PDF作成")).all()
        assert [(h.actor_email, h.actor_name, h.comment) for h in hist] == [
            ("sup1@lineworks-local.info", "sup1", "https://drive.google.com/file/d/F1/view")]


def test_regenerate_pdf_is_guarded(env):
    env.set_request(status="完了", current_step="完了済")
    jid = env.enqueue("regeneratePdf", [RID, ACTOR])
    env.set_request(status="取消")
    assert env.runner.run_once()["skipped"] == 1
    assert env.renderer.count == 0 and env.job(jid).state == "skipped"


def test_trash_pdf_is_not_guarded_and_sends_no_mail(env):
    jid = env.enqueue("trashPdf", ["P9", RID])  # guard を渡しても照合しない
    assert (env.job(jid).request_id, env.job(jid).guard_status, env.job(jid).guard_step) == (RID, "", "")
    env.set_request(status="取消", pdf_file_id="", pdf_url="")
    assert env.runner.run_once()["done"] == 1
    assert env.storage.trashed == ["P9"] and env.renderer.count == 0
    assert env.mails(jid) == [] and env.transport.sent == []


def test_trash_pdf_goes_through_safety_in_staging(env):
    from test_worker_drive import make_storage

    env.runner.storage, sess = make_storage({"PROD_PDF": ["PROD_DATE"], "PROD_DATE": ["prod-folder-id"],
                                             "STG_PDF": ["STG_DATE"], "STG_DATE": ["staging-folder-id"]})
    a = env.enqueue("trashPdf", ["PROD_PDF", RID])
    b = env.enqueue("trashPdf", ["STG_PDF", RID])
    env.runner.run_once()
    env.runner.run_once()
    assert env.job(a).state == "done" and env.job(b).state == "done"
    # 本番の PDF（移したデータが指す）は消さない。staging の置き場の中だけゴミ箱へ。
    assert [c[1].rsplit("/", 1)[1] for c in sess.calls if c[0] == "PATCH"] == ["STG_PDF"]


def test_outbox_defer_uses_enqueue_for_new_kinds(env, session_factory):
    from app.domain import outbox

    with session_factory() as db:
        req = db.get(models.Request, RID)
        req.status, req.current_step = "完了", "完了済"
        regen = outbox.defer(db, "regeneratePdf", [RID, ACTOR], guard=req)
        trash = outbox.defer(db, "trashPdf", ["P9", RID], request_id=RID)
        assert regen.id and trash.id  # flush 済み
        db.commit()
        assert (regen.request_id, regen.guard_status, regen.guard_step) == (RID, "完了", "完了済")
        assert (trash.request_id, trash.guard_status, trash.guard_step) == (RID, "", "")
        assert json.loads(trash.args_json) == ["P9", RID]
    assert jobs.JOB_LABELS["regeneratePdf"] and jobs.JOB_LABELS["trashPdf"]


# ---------------------------------------------------------------------------
# 送り直しの決まり（レビュー指摘の修正）
# ---------------------------------------------------------------------------


def test_timed_out_send_is_checked_by_message_id_before_resending(env):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.lose_response = 1  # Gmail には届いたが、応答が来なかった
    assert env.runner.run_once()["retry"] == 1
    (m,) = env.mails(jid)
    assert m.sent is False and m.attempts == 1 and m.message_id_header.startswith("<")
    assert env.transport.sent[0]["Message-ID"] == m.message_id_header
    assert env.transport.sent[0]["X-Chozouhin-Mail-Id"] == m.message_id_header
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["done"] == 1
    assert len(env.transport.sent) == 1  # 送り直さない
    assert env.transport.find_calls == [m.message_id_header]
    (m,) = env.mails(jid)
    assert m.sent and m.gmail_message_id == "gm-1"


def test_failed_send_is_resent_with_the_same_message_id(env):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.fail_to = {"applicant@lineworks-local.info"}
    env.runner.run_once()
    first_id = env.mails(jid)[0].message_id_header
    env.transport.fail_to = set()
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["done"] == 1
    assert len(env.transport.sent) == 1 and env.transport.sent[0]["Message-ID"] == first_id
    assert env.transport.find_calls == [first_id]


def test_unverifiable_send_is_not_resent(env):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.lose_response = 1
    env.runner.run_once()

    def broken(message_id):
        raise RuntimeError("403 insufficient scope")

    env.transport.find_sent = broken
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["retry"] == 1
    assert len(env.transport.sent) == 1  # 確かめられないうちは送らない
    assert "insufficient scope" in env.mails(jid)[0].last_error


def test_record_failure_after_send_is_not_resent_and_admin_is_told(env, monkeypatch):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    real_update = env.runner.store.update_mail

    def update_mail(mail_id, now, **fields):
        if fields.get("sent"):
            raise RuntimeError("db write failed")
        return real_update(mail_id, now, **fields)

    monkeypatch.setattr(env.runner.store, "update_mail", update_mail)
    assert env.runner.run_once()["done"] == 1
    monkeypatch.setattr(env.runner.store, "update_mail", real_update)
    assert len([m for m in env.transport.sent if "差戻し" in str(m["Subject"])]) == 1
    admin = [m for m in env.mails() if m.job_id is None]
    assert len(admin) == 1 and "記録に失敗しました（送り直しはしません）" in admin[0].body_text
    env.clock.advance(hours=3)
    assert env.runner.run_once() == {"done": 0, "skipped": 0, "retry": 0, "failed": 0}
    assert env.job(jid).state == "done"


def test_skip_with_failed_unsent_mail_tells_admin(env):
    jid = env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.fail_to = {"applicant@lineworks-local.info"}
    env.runner.run_once()
    env.set_request(current_step="購買見積")
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["skipped"] == 1
    admin = [m for m in env.mails() if m.job_id is None]
    assert len(admin) == 1 and admin[0].kind == "admin_job_failure"
    assert admin[0].body_text.startswith("送れなかった通知メールが残ったまま")
    assert env.job(jid).state == "skipped"


def test_skip_without_failed_mail_does_not_tell_admin(env):
    env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "上席", "購買見積", {"email": "mech@x"}, ACTOR])
    env.set_request(current_step="購買見積")
    assert env.runner.run_once()["skipped"] == 1
    assert [m for m in env.mails() if m.job_id is None] == []


def test_notification_setting_is_checked_right_before_sending(env, session_factory):
    env.set_request(current_step="購買見積")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "上席", "購買見積", {"email": "mech@x"}, ACTOR])

    def turn_off():
        with session_factory() as db:
            db.get(models.Setting, "enableEmailNotifications").value = "false"
            db.commit()

    env.storage.on_save = turn_off  # メールを組み立てた後・送る前に通知を止めた
    assert env.runner.run_once()["done"] == 1
    assert env.transport.sent == []
    assert all(not m.sent and m.last_error.startswith("skipped:") for m in env.mails(jid))


def test_test_redirect_expiry_is_checked_right_before_sending(env, session_factory):
    env.runner.config = MailPdfConfig(app_env="production", public_origin=ORIGIN)
    with session_factory() as db:
        until = int((env.clock() + timedelta(minutes=3)).timestamp() * 1000)
        db.add(models.Setting(key="mailRedirectTo", value=f"imaizumi@lineworks-local.info|{until}"))
        db.commit()
    env.enqueue("sendReturnedEmail_", [snapshot(env), "c"])
    env.transport.fail_to = {"applicant@lineworks-local.info"}
    env.runner.run_once()
    env.transport.fail_to = set()
    env.clock.advance(minutes=5)  # 送り直すときにはテスト転送が切れている
    env.runner.run_once()
    assert env.transport.sent[-1]["To"] == "applicant@lineworks-local.info"


def test_pdf_is_not_remade_when_mail_building_fails(env, monkeypatch):
    env.set_request(current_step="購買手配")
    jid = env.enqueue("dispatchApprovalNotifications_", [snapshot(env), "総務部長", "購買手配", {}, ACTOR])
    real_build = env.runner._build_mails
    calls = {"n": 0}

    def flaky(job, masters):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("build failed")
        return real_build(job, masters)

    monkeypatch.setattr(env.runner, "_build_mails", flaky)
    assert env.runner.run_once()["retry"] == 1
    assert env.job(jid).pdf_file_id == "F1"
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["done"] == 1
    assert env.renderer.count == 1 and len(env.storage.saved) == 1  # PDF は 1 回だけ
    (m,) = env.mails(jid)
    assert m.attachment_file_id == "F1" and m.sent
