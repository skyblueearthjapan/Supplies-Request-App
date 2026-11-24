"""jobs: 照合・PDF→メールの段分け・未送信分だけの送り直し・管理者通知・同じ申請は同時に 1 本（SQLite）。

jobs / mail_log のモデルは担当 F が models.py に作る。ここでは SqlJobStore に要る列だけのモデルをテスト用に作る。
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from mailpdf_fixtures import ORIGIN, make_items, make_masters, make_request

from app.jobs import MAX_ATTEMPTS, JobKind, JobRunner, RecordPdfResult, SqlJobStore
from app.services.drive import DriveFile
from app.services.mailpdf_common import MailPdfConfig


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(50))
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    guard_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    guard_step: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state: Mapped[str] = mapped_column(String(20), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    mails_built: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MailLog(Base):
    __tablename__ = "mail_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    request_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(50))
    to_addrs: Mapped[str] = mapped_column(Text)
    cc_addrs: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    attach_pdf: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attachment_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    gmail_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    redirected_to: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# 偽物
# ---------------------------------------------------------------------------

class FakeGateway:
    def __init__(self, masters=None):
        self.requests: dict[str, dict] = {}
        self.items: dict[str, list] = {}
        self.history: dict[str, list] = {}
        self.masters = masters or make_masters()
        self.on_record_pdf = None  # PDF を書く直前に割り込む（作っている間の取消など）

    def add(self, req, items=None, history=None):
        self.requests[req["requestId"]] = req
        self.items[req["requestId"]] = items if items is not None else make_items()
        self.history[req["requestId"]] = history or [
            {"action": "申請", "fromStep": "", "toStep": "上席", "happenedAt": "2026-11-20 09:00:00",
             "actorEmail": req["applicantEmail"], "actorName": req["applicantName"]}]

    def get_request(self, rid):
        r = self.requests.get(rid)
        return copy.deepcopy(r) if r else None

    def get_items(self, rid):
        return copy.deepcopy(self.items.get(rid, []))

    def get_history(self, rid):
        return copy.deepcopy(self.history.get(rid, []))

    def get_masters(self):
        return self.masters

    def record_pdf(self, rid, file_id, url, actor):
        if self.on_record_pdf:
            self.on_record_pdf()
        cur = self.requests.get(rid)
        if not cur or cur["status"] == "取消":
            return RecordPdfResult(stored=False)
        replaced = cur.get("pdfFileId") or ""
        cur["pdfFileId"], cur["pdfUrl"] = file_id, url
        self.history[rid].append({"action": "PDF作成", "fromStep": cur["currentStep"], "toStep": cur["currentStep"],
                                  "happenedAt": "2026-11-24 10:00:00", "actorEmail": (actor or {}).get("email", ""),
                                  "actorName": (actor or {}).get("name", ""), "comment": url})
        return RecordPdfResult(stored=True, replaced_file_id="" if replaced == file_id else replaced)


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.fail_to: set[str] = set()  # この宛先（本来の To）を含むメールは失敗させる

    def send(self, msg):
        body = msg.get_body(("plain",)).get_content()
        if any(addr in body for addr in self.fail_to):
            raise RuntimeError("smtp down")
        self.sent.append(msg)
        return f"gm-{len(self.sent)}"


class FakeStorage:
    def __init__(self):
        self.saved: list[tuple[str, str]] = []
        self.trashed: list[str] = []
        self.downloads = 0

    def save(self, settings, request_date, name, data):
        fid = f"F{len(self.saved) + 1}"
        self.saved.append((fid, name))
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
        self.t = datetime(2026, 11, 24, 1, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def env():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    store = SqlJobStore(Session, Job, MailLog)
    gw = FakeGateway()
    clock = Clock()
    runner = JobRunner(store=store, gateway=gw, transport=FakeTransport(), storage=FakeStorage(), renderer=FakeRenderer(),
                       config=MailPdfConfig(app_env="staging", public_origin=ORIGIN, mail_redirect_to="imaizumi@lineworks-local.info"),
                       extra_admins=["imaizumi@lineworks-local.info"], now=clock)

    class Env:
        pass

    e = Env()
    e.Session, e.store, e.gw, e.clock, e.runner = Session, store, gw, clock, runner
    e.transport, e.storage, e.renderer = runner.transport, runner.storage, runner.renderer

    def enqueue(kind, rid, payload, guard=True):
        with Session() as s, s.begin():
            req = gw.get_request(rid)
            g = {"status": req["status"], "currentStep": req["currentStep"]} if (guard and req) else None
            job = store.enqueue(s, kind, rid, payload, g, now=clock())
            s.flush()
            return job.id

    e.enqueue = enqueue

    def mails(job_id=None):
        with Session() as s:
            q = s.query(MailLog)
            if job_id is not None:
                q = q.filter(MailLog.job_id == job_id)
            return q.order_by(MailLog.id).all()

    def job(job_id):
        with Session() as s:
            return s.get(Job, job_id)

    e.mails, e.job = mails, job
    return e


def original_to(msg):
    body = msg.get_body(("plain",)).get_content()
    return [line for line in body.splitlines() if line.startswith("本来の To")][0]


# ---------------------------------------------------------------------------

def test_enqueue_counts_pending_and_runs(env):
    req = make_request()
    env.gw.add(req)
    sup = {"email": "sup1@lineworks-local.info", "name": "上田 一郎", "title": "課長", "notifyEmail": ""}
    jid = env.enqueue(JobKind.APPROVAL_REQUEST, req["requestId"], {"request": req, "approvers": [sup]})
    with env.Session() as s:
        assert env.store.count_pending(s, req["requestId"]) == 1
    assert env.runner.run_once() == {"done": 1, "skipped": 0, "retry": 0, "failed": 0}
    logs = env.mails(jid)
    assert [(m.kind, m.to_addrs, m.state, m.redirected_to) for m in logs] == [
        ("approval_request", "sup1@lineworks-local.info", "sent", "imaizumi@lineworks-local.info")]
    assert logs[0].gmail_message_id == "gm-1"
    assert env.transport.sent[0]["To"] == "imaizumi@lineworks-local.info"  # staging は必ず転送
    with env.Session() as s:
        assert env.store.count_pending(s, req["requestId"]) == 0
    assert env.runner.run_once()["done"] == 0  # 2 回は動かない


def test_guard_skips_when_state_changed(env):
    req = make_request()
    env.gw.add(req)
    jid = env.enqueue(JobKind.APPROVAL_DISPATCH, req["requestId"],
                      {"request": req, "curKey": "上席", "nextStep": "購買見積", "actor": {"email": "sup1@x", "name": "sup1"}})
    env.gw.requests[req["requestId"]]["currentStep"] = "購買見積"  # 承認の直後にリコールなどで段が変わった
    assert env.runner.run_once()["skipped"] == 1
    assert env.renderer.count == 0 and env.transport.sent == [] and env.mails(jid) == []
    assert env.job(jid).state == "skipped"


def test_broadcast_is_not_guarded(env):
    req = make_request(totalAmount=3600)
    env.gw.add(req)
    jid = env.enqueue(JobKind.RETURNED_BROADCAST, req["requestId"],
                      {"request": req, "stepAtReturn": "上席", "comment": "直して", "actorName": "上田 一郎"})
    env.gw.requests[req["requestId"]].update(status="差戻し", currentStep="申請者")
    assert env.runner.run_once()["done"] == 1
    assert [m.kind for m in env.mails(jid)] == ["returned_broadcast"]


def test_dispatch_to_quote_makes_pdf_then_mail_with_attachment(env):
    req = make_request(currentStep="購買見積", pdfFileId="OLD", pdfUrl="https://old")
    env.gw.add(req)
    jid = env.enqueue(JobKind.APPROVAL_DISPATCH, req["requestId"],
                      {"request": req, "curKey": "上席", "nextStep": "購買見積", "actor": {"email": "sup1@x", "name": "sup1"}})
    assert env.runner.run_once()["done"] == 1
    assert env.renderer.count == 1
    assert env.storage.saved == [("F1", "REQ-20261124093000-123456_申請 太郎_貯蔵品購入申請書.pdf")]
    assert env.storage.trashed == ["OLD"]  # 1 申請 = 1 ファイル
    assert env.gw.requests[req["requestId"]]["pdfFileId"] == "F1"
    (log,) = env.mails(jid)
    assert log.kind == "quote_request" and log.attach_pdf and log.pdf_file_id == "F1"
    msg = env.transport.sent[0]
    att = [p for p in msg.iter_attachments()]
    assert att[0].get_filename() == "REQ-20261124093000-123456_申請 太郎_貯蔵品購入申請書.pdf"
    assert att[0].get_content() == b"%PDF-1.4 fake"
    assert env.job(jid).pdf_file_id == "F1"


def test_complete_job_mails_have_pdf_url_and_attachment(env):
    req = make_request(status="完了", currentStep="完了済", totalAmount=3600)
    env.gw.add(req)
    jid = env.enqueue(JobKind.COMPLETE, req["requestId"], {"request": req, "actor": {"email": "mech@x", "name": "mech"}})
    env.runner.run_once()
    logs = env.mails(jid)
    assert [(m.kind, m.attach_pdf) for m in logs] == [("ga_arranged", True), ("completed", False)]
    assert "PDF: https://drive.google.com/file/d/F1/view" in logs[1].body  # PDF を作った後の行で組み立てる


def test_cancelled_while_making_pdf_discards_pdf_and_mail(env):
    req = make_request(currentStep="購買手配")
    env.gw.add(req)
    jid = env.enqueue(JobKind.APPROVAL_DISPATCH, req["requestId"],
                      {"request": req, "curKey": "総務部長", "nextStep": "購買手配", "actor": {}})
    env.gw.on_record_pdf = lambda: env.gw.requests[req["requestId"]].update(status="取消")
    assert env.runner.run_once()["skipped"] == 1
    assert env.storage.trashed == ["F1"]  # 作った PDF はゴミ箱
    assert env.transport.sent == [] and env.mails(jid) == []


def test_retry_resends_only_unsent_mails_with_backoff_then_notifies_admin(env):
    req = make_request(currentStep="総務部長")
    env.gw.add(req)
    jid = env.enqueue(JobKind.APPROVAL_DISPATCH, req["requestId"],
                      {"request": req, "curKey": "購買見積", "nextStep": "総務部長", "nextApprover": {"email": "gm1@lineworks-local.info"}})
    env.transport.fail_to = {"本来の To : gm2@lineworks-local.info"}
    assert env.runner.run_once()["retry"] == 1
    states = [(m.to_addrs, m.state) for m in env.mails(jid)]
    assert states == [("gm1@lineworks-local.info", "sent"), ("gm2@lineworks-local.info", "error")]
    assert len(env.transport.sent) == 1
    # 5 分たつまでは動かない
    env.clock.advance(minutes=4)
    assert env.runner.run_once() == {"done": 0, "skipped": 0, "retry": 0, "failed": 0}
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
    assert j.state == "failed" and j.attempts == MAX_ATTEMPTS == 4
    assert [m.attempts for m in env.mails(jid)] == [1, 4]
    admin = [m for m in env.mails() if m.job_id is None]
    assert len(admin) == 1 and admin[0].kind == "admin_job_failure" and admin[0].state == "sent"
    assert admin[0].to_addrs == "imaizumi@lineworks-local.info"
    assert env.store.list_failed()[0]["requestId"] == req["requestId"]
    assert env.store.clear_failed() == 1 and env.store.list_failed() == []


def test_retry_succeeds_and_does_not_rebuild_or_remake_pdf(env):
    req = make_request(currentStep="購買手配")
    env.gw.add(req)
    jid = env.enqueue(JobKind.APPROVAL_DISPATCH, req["requestId"],
                      {"request": req, "curKey": "総務部長", "nextStep": "購買手配", "actor": {}})
    env.transport.fail_to = {"本来の To : mech@lineworks-local.info"}
    assert env.runner.run_once()["retry"] == 1
    env.transport.fail_to = set()
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["done"] == 1
    assert env.renderer.count == 1 and len(env.storage.saved) == 1  # PDF は作り直さない
    assert env.storage.downloads == 1  # 添付はドライブから読み直す
    assert len(env.mails(jid)) == 1 and env.mails(jid)[0].state == "sent"


def test_guard_checked_again_on_retry(env):
    req = make_request()
    env.gw.add(req)
    jid = env.enqueue(JobKind.RETURNED, req["requestId"], {"request": req, "comment": "c"})
    env.transport.fail_to = {"本来の To : applicant@lineworks-local.info"}
    env.runner.run_once()
    env.gw.requests[req["requestId"]].update(status="承認中", currentStep="購買見積")
    env.clock.advance(minutes=5)
    assert env.runner.run_once()["skipped"] == 1
    assert [m.state for m in env.mails(jid)] == ["skipped"]


def test_same_request_runs_one_at_a_time_in_order(env):
    req = make_request()
    env.gw.add(req)
    other = make_request(requestId="REQ-OTHER")
    env.gw.add(other)
    a = env.enqueue(JobKind.RETURNED, req["requestId"], {"request": req, "comment": "1"})
    b = env.enqueue(JobKind.REJECTED, req["requestId"], {"request": req, "comment": "2"})
    c = env.enqueue(JobKind.RETURNED, other["requestId"], {"request": other, "comment": "3"})
    claimed = env.store.claim(env.clock(), limit=10)
    assert [j.id for j in claimed] == [a, c]  # 同じ申請の 2 本目は後回し
    assert env.store.claim(env.clock(), limit=10) == []  # a が動いている間は b を取らない
    env.store.update_job(a, state="done")
    assert [j.id for j in env.store.claim(env.clock(), limit=10)] == [b]


def test_stale_running_job_is_reclaimed(env):
    req = make_request()
    env.gw.add(req)
    a = env.enqueue(JobKind.RETURNED, req["requestId"], {"request": req, "comment": "1"})
    assert [j.id for j in env.store.claim(env.clock())] == [a]
    env.clock.advance(minutes=16)  # worker が止まった
    (j,) = env.store.claim(env.clock())
    assert j.id == a and j.attempts == 2


def test_split_returned_and_regenerate_pdf(env):
    orig = make_request(currentStep="社長", pdfFileId="P0", status="承認中")
    new = make_request(requestId="REQ-NEW", status="差戻し", currentStep="申請者")
    env.gw.add(orig)
    env.gw.add(new)
    j1 = env.enqueue(JobKind.SPLIT_RETURNED, orig["requestId"],
                     {"newRequestId": "REQ-NEW", "stepAtSplit": "総務部長", "comment": "分けた", "actorName": "辻野"}, guard=False)
    j2 = env.enqueue(JobKind.REGENERATE_PDF_AFTER_SPLIT, orig["requestId"], {"request": orig, "nextStep": "社長", "actor": {}})
    j3 = env.enqueue(JobKind.REGENERATE_PDF_AFTER_SPLIT, orig["requestId"], {"request": orig, "nextStep": "購買手配", "actor": {}})
    for _ in range(3):
        env.runner.run_once()
    assert [m.kind for m in env.mails(j1)] == ["split_returned", "split_returned_broadcast"]
    assert env.job(j2).pdf_file_id == "F1" and env.storage.trashed == ["P0"]
    assert env.job(j3).pdf_file_id is None  # 遷移先で作るので作らない
    assert env.renderer.count == 1


def test_production_without_redirect_sends_to_real_recipients(env):
    env.runner.config = MailPdfConfig(app_env="production", public_origin=ORIGIN)
    req = make_request()
    env.gw.add(req)
    env.enqueue(JobKind.RETURNED, req["requestId"], {"request": req, "comment": "c"})
    env.runner.run_once()
    assert env.transport.sent[0]["To"] == "applicant@lineworks-local.info"
    # 管理画面のテスト転送（設定 mailRedirectTo）が有効なら本番でも寄せる
    env.gw.masters.settings["mailRedirectTo"] = "imaizumi@lineworks-local.info|" + str(int((env.clock() + timedelta(hours=1)).timestamp() * 1000))
    env.enqueue(JobKind.RETURNED, req["requestId"], {"request": req, "comment": "c"})
    env.runner.run_once()
    assert env.transport.sent[1]["To"] == "imaizumi@lineworks-local.info"


def test_enqueue_rejects_unknown_kind(env):
    with env.Session() as s, pytest.raises(ValueError):
        env.store.enqueue(s, "sendMail", "REQ-1", {})
