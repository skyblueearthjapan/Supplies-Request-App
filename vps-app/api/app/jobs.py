"""後回しの仕事（outbox）: PDF 作成とメール送信。GAS 版 Jobs.gs と Workflow.gs の送信部分の移植。

取り決め（担当 W と）:
- W は状態の変更と同じトランザクションで enqueue() で積む（domain/outbox.defer が呼ぶ）。
- kind は GAS 版 DEFERRABLE_JOBS と同じ関数名、args は GAS 版 defer_ と同じ並び
  （申請行は serialize.request_row の dict、承認者は {email,name,title,notifyEmail}、user は {email,name}）。
  VPS 版で足した kind: regeneratePdf [requestId, {email,name}]（PDF だけ作り直す。メールなし）、
  trashPdf [pdfFileId, requestId]（PDF をゴミ箱へ。照合なし。safety を通る）。
- guard は差戻し共有（sendReturnedBroadcast_）・分割通知（notifySplitReturned_）・trashPdf だけ空。

GAS 版からの変更（計画書 9-2・実装仕様 3 章）:
- メールは mail_log に 1 通ずつ記録し、送れなかった分（sent=false）だけを 5 分・30 分・2 時間後に送り直す。
  それでも失敗したら管理者へ知らせる。
- 実行のたびに申請の状態と段を照合し、変わっていれば出さずに捨てる（skipped）。
- 同じ申請の仕事は同時に 1 本だけ（同じ申請の PDF を 2 本同時に作らない）。積んだ順に動かす。
- 取り出しは SELECT ... FOR UPDATE SKIP LOCKED（SQLite では SQLAlchemy が出さない）。

段の分け方: ①PDF（要る仕事だけ）→ ②メールを組み立てて mail_log に積む（1 回だけ）→ ③未送信の分を送る。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app import models, serialize
from app.services.drive import PdfStorage
from app.services.mail import MailTransport, effective_redirect, send_mail
from app.services.mailpdf_common import ACTION, STATUS, STEPS, MailPdfConfig, Masters, js_str
from app.services.pdf import PdfRenderer, build_pdf_view, pdf_file_name, render_pdf_html
from app.services.recipients import MailBuilder, MailKind, OutgoingMail, job_failure_admin_mail
from app.timeutil import new_id, now_utc

log = logging.getLogger(__name__)

RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1  # 最初の 1 回 + 送り直し 3 回
RUNNING_LEASE = timedelta(minutes=15)  # これより長く running のままなら worker が止まったとみなして拾い直す
SETTINGS_REDIRECT_KEY = "mailRedirectTo"  # 本番のテスト転送（設定の値「email|失効エポックミリ秒」）

# 後回しにできる仕事（GAS 版 DEFERRABLE_JOBS）と管理画面の表示名
JOB_LABELS = {
    "sendApprovalRequestEmail_": "承認依頼メール",
    "sendRequestUpdatedEmail_": "内容更新メール",
    "sendRejectedEmail_": "却下メール",
    "sendReturnedEmail_": "差戻しメール",
    "sendReturnedBroadcast_": "差戻し共有メール",
    "notifyGeneralAffairs_": "総務部への通知メール",
    "notifySplitReturned_": "分割の差戻しメール",
    "dispatchApprovalNotifications_": "承認後の通知・PDF",
    "completeAndNotify_": "完了の通知・PDF",
    "regeneratePdfAfterSplit_": "分割後の PDF 作り直し",
    # VPS 版で足したもの（担当 W が積む）
    "regeneratePdf": "PDF再作成",  # [requestId, {email, name}]。手動の「PDF再作成」。メールは送らない
    "trashPdf": "PDFをゴミ箱へ移す",  # [pdfFileId, requestId]。取消・却下した申請の PDF
}
UNGUARDED_KINDS = {"sendReturnedBroadcast_", "notifySplitReturned_", "trashPdf"}


# ---------------------------------------------------------------------------
# 積む（W）・数える・管理画面
# ---------------------------------------------------------------------------

def enqueue(db: Session, kind: str, args: Sequence[Any], request_id: str = "",
            guard: Mapping[str, Any] | None = None, now: datetime | None = None) -> models.Job:
    """仕事を 1 件積む（commit は呼び出し側）。guard は積んだ時点の申請 {status, currentStep}。"""
    if kind not in JOB_LABELS:
        raise ValueError(f"後回しにできない関数です: {kind}")
    now = now or now_utc()
    guarded = kind not in UNGUARDED_KINDS and guard is not None
    job = models.Job(kind=kind, args_json=json.dumps(list(args), ensure_ascii=False, default=str),
                     request_id=request_id or _request_id_from_args(kind, args),
                     guard_status=(guard.get("status") or "") if guarded else "",
                     guard_step=(guard.get("currentStep") or "") if guarded else "",
                     state="pending", attempts=0, last_error="", run_after=now, created_at=now, updated_at=now)
    db.add(job)
    return job


def _request_id_from_args(kind: str, args: Sequence[Any]) -> str:
    if kind == "trashPdf":
        return js_str(args[1]) if len(args) > 1 else ""
    first = args[0] if args else None
    if isinstance(first, Mapping):
        return js_str(first.get("requestId"))
    return js_str(first)


def pending_count(db: Session, request_id: str) -> int:
    """詳細画面の pendingJobs（まだ終わっていない仕事の数）。"""
    J = models.Job
    return db.scalar(select(func.count()).select_from(J).where(
        J.request_id == request_id, J.state.in_(("pending", "running")))) or 0


def list_failed(db: Session) -> list[dict]:
    """管理画面「失敗した後処理」（GAS 版 listFailedJobs の代わり）。"""
    J = models.Job
    rows = db.scalars(select(J).where(J.state == "failed").order_by(J.id)).all()
    return [{"jobId": r.id, "kind": r.kind, "label": JOB_LABELS.get(r.kind, r.kind), "requestId": r.request_id,
             "failedAt": serialize.fmt_datetime(r.finished_at), "attempts": r.attempts, "error": r.last_error or ""}
            for r in rows]


def retry_failed(db: Session, job_id: int, now: datetime | None = None) -> bool:
    """失敗した仕事をもう一度動かす（未送信のメールだけ送る。送り直しの回数は数え直す）。commit は呼び出し側。"""
    job = db.get(models.Job, job_id)
    if job is None or job.state != "failed":
        return False
    now = now or now_utc()
    job.state, job.attempts, job.run_after, job.finished_at, job.updated_at = "pending", 0, now, None, now
    return True


# ---------------------------------------------------------------------------
# 申請・マスタの読み書き（DB）
# ---------------------------------------------------------------------------

@dataclass
class RecordPdfResult:
    stored: bool  # False: 取消済み・無い申請だったので書かなかった（作った PDF は捨てる）
    replaced_file_id: str = ""  # 書いた時点で申請が指していた PDF


class DbRequestGateway:
    """jobs が読む申請・明細・履歴・マスタと、PDF の記録（GAS 版 writePdfResult_）。"""

    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def get_request(self, request_id: str) -> dict | None:
        with self.session_factory() as s:
            r = s.get(models.Request, request_id)
            return serialize.request_row(r) if r else None

    def get_items(self, request_id: str) -> list[dict]:
        I = models.RequestItem
        with self.session_factory() as s:
            rows = s.scalars(select(I).where(I.request_id == request_id).order_by(I.line_no, I.item_id)).all()
            return [serialize.item_row(r) for r in rows]

    def get_history(self, request_id: str) -> list[dict]:
        H = models.History
        with self.session_factory() as s:
            rows = s.scalars(select(H).where(H.request_id == request_id).order_by(H.happened_at, H.seq)).all()
            return [serialize.history_row(r) for r in rows]

    def get_masters(self) -> Masters:
        with self.session_factory() as s:
            def ordered(model):
                return s.scalars(select(model).order_by(model.sort_order, model.id)).all()

            return Masters(
                approver_rules=[serialize.approver_row(r) for r in ordered(models.ApproverRule)],
                dept_supervisors=[serialize.dept_supervisor_row(r) for r in ordered(models.DeptSupervisor)],
                recipients=[serialize.recipient_row(r) for r in ordered(models.NotificationRecipient)],
                settings=serialize.settings_map(s.scalars(select(models.Setting)).all()),
            )

    def record_pdf(self, request_id: str, file_id: str, url: str, actor: Mapping | None) -> RecordPdfResult:
        """行ロックを取って読み直し、取消でなければ pdfFileId / pdfUrl を書いて履歴「PDF作成」を足す。"""
        with self.session_factory() as s, s.begin():
            r = s.scalars(select(models.Request).where(models.Request.request_id == request_id).with_for_update()).first()
            if r is None or r.status == STATUS.CANCELLED:
                return RecordPdfResult(stored=False)
            replaced = r.pdf_file_id or ""
            now = now_utc()
            r.updated_at, r.pdf_file_id, r.pdf_url = now, file_id, url
            r.row_version = (r.row_version or 0) + 1
            s.add(models.History(history_id=new_id("HIS", now), request_id=request_id, happened_at=now,
                                 actor_email=js_str((actor or {}).get("email")), actor_name=js_str((actor or {}).get("name")),
                                 action=ACTION.PDF_GENERATE, from_status=r.status, to_status=r.status,
                                 from_step=r.current_step, to_step=r.current_step, comment=url))
            return RecordPdfResult(stored=True, replaced_file_id="" if replaced == file_id else replaced)


# ---------------------------------------------------------------------------
# 取り出し・mail_log
# ---------------------------------------------------------------------------

@dataclass
class JobView:
    id: int
    kind: str
    request_id: str
    args: list
    guard_status: str
    guard_step: str
    attempts: int


class JobStore:
    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def claim(self, now: datetime | None = None, limit: int = 10) -> list[JobView]:
        now = now or now_utc()
        J = models.Job
        stale = now - RUNNING_LEASE
        due_now = or_(J.run_after.is_(None), J.run_after <= now)
        claimed: list[JobView] = []
        with self.session_factory() as s, s.begin():
            candidates = s.scalars(
                select(J).where(or_(and_(J.state == "pending", due_now), and_(J.state == "running", J.updated_at < stale)))
                .order_by(J.id).limit(limit * 3).with_for_update(skip_locked=True)).all()
            taken: set[str] = set()
            for job in candidates:
                if len(claimed) >= limit:
                    break
                if job.request_id in taken:
                    continue
                # 同じ申請の、動いている仕事か、先に積まれて今すぐ動かす仕事があれば後回し（同時に 1 本・積んだ順）。
                busy = s.scalar(select(func.count()).select_from(J).where(
                    J.request_id == job.request_id, J.id != job.id,
                    or_(and_(J.state == "running", J.updated_at >= stale),
                        and_(J.state == "pending", due_now, J.id < job.id)))) or 0
                if busy:
                    continue
                job.state, job.updated_at, job.attempts = "running", now, (job.attempts or 0) + 1
                taken.add(job.request_id)
                try:
                    args = json.loads(job.args_json or "[]")
                except ValueError:
                    args = []
                claimed.append(JobView(id=job.id, kind=job.kind, request_id=job.request_id,
                                       args=args if isinstance(args, list) else [], guard_status=job.guard_status or "",
                                       guard_step=job.guard_step or "", attempts=job.attempts))
        return claimed

    def update_job(self, job_id: int, now: datetime, **fields: Any) -> None:
        with self.session_factory() as s, s.begin():
            job = s.get(models.Job, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = now

    def has_mails(self, job_id: int) -> bool:
        with self.session_factory() as s:
            return bool(s.scalar(select(func.count()).select_from(models.MailLog).where(models.MailLog.job_id == job_id)))

    def add_mails(self, job_id: int, mails: Sequence[OutgoingMail], pdf_file_id: str, now: datetime) -> None:
        with self.session_factory() as s, s.begin():
            if s.scalar(select(func.count()).select_from(models.MailLog).where(models.MailLog.job_id == job_id)):
                return  # 2 回は積まない
            for m in mails:
                s.add(models.MailLog(job_id=job_id, request_id=m.request_id, kind=m.kind, to_addrs=", ".join(m.to),
                                     cc_addrs=", ".join(m.cc), subject=m.subject, body_html="", body_text=m.body,
                                     attachment_file_id=pdf_file_id if m.attach_pdf else "", redirected_to="",
                                     sent=False, gmail_message_id="", attempts=0, last_error="",
                                     created_at=now, updated_at=now))

    def unsent_mails(self, job_id: int) -> list[dict]:
        M = models.MailLog
        with self.session_factory() as s:
            rows = s.scalars(select(M).where(M.job_id == job_id, M.sent.is_(False)).order_by(M.id)).all()
            return [{"id": r.id, "kind": r.kind, "request_id": r.request_id, "to": r.to_addrs, "cc": r.cc_addrs,
                     "subject": r.subject, "body": r.body_text, "attachment_file_id": r.attachment_file_id,
                     "attempts": r.attempts or 0} for r in rows]

    def update_mail(self, mail_id: int, now: datetime, **fields: Any) -> None:
        with self.session_factory() as s, s.begin():
            row = s.get(models.MailLog, mail_id)
            for k, v in fields.items():
                setattr(row, k, v)
            row.updated_at = now

    def mark_unsent_skipped(self, job_id: int, reason: str, now: datetime) -> None:
        """照合で捨てた仕事の未送信分に理由を残す（sent=false のまま。仕事が skipped なので送り直さない）。"""
        M = models.MailLog
        with self.session_factory() as s, s.begin():
            for r in s.scalars(select(M).where(M.job_id == job_id, M.sent.is_(False))).all():
                r.last_error, r.updated_at = ("skipped: " + reason)[:1000], now

    def log_direct_mail(self, mail: OutgoingMail, sent: bool, now: datetime, message_id: str = "", error: str = "",
                        redirected_to: str = "") -> None:
        with self.session_factory() as s, s.begin():
            s.add(models.MailLog(job_id=None, request_id=mail.request_id, kind=mail.kind, to_addrs=", ".join(mail.to),
                                 cc_addrs=", ".join(mail.cc), subject=mail.subject, body_html="", body_text=mail.body,
                                 attachment_file_id="", redirected_to=redirected_to, sent=sent,
                                 sent_at=now if sent else None, gmail_message_id=message_id, attempts=1,
                                 last_error=error, created_at=now, updated_at=now))


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

class SkipJob(Exception):
    """状態が変わった・取消された等で、出さずに捨てる。"""


def _arg(args: list, i: int, default: Any = None) -> Any:
    return args[i] if len(args) > i and args[i] is not None else default


def _ga_kind(subject: str) -> str:
    """notifyGeneralAffairs_ の件名から mail_log の種類を決める（W が件名を作って渡すため）。"""
    if "手配完了" in subject:
        return MailKind.GA_ARRANGED
    if "⚡" in subject:
        return MailKind.GA_EXPEDITED
    if "🚨" in subject:
        return MailKind.GA_QUOTE_CONFIRMED_OVER
    return MailKind.GA_QUOTE_CONFIRMED


@dataclass
class JobRunner:
    store: JobStore
    gateway: Any  # DbRequestGateway と同じ形（テストは偽物）
    transport: MailTransport
    storage: PdfStorage
    renderer: PdfRenderer
    config: MailPdfConfig
    extra_admins: Sequence[str] = ()  # BOOTSTRAP_ADMINS
    now: Callable[[], datetime] = now_utc
    _pdf_cache: dict[str, bytes] = field(default_factory=dict)

    def run_once(self, limit: int = 10) -> dict[str, int]:
        result = {"done": 0, "skipped": 0, "retry": 0, "failed": 0}
        for job in self.store.claim(self.now(), limit):
            result[self.run_job(job)] += 1
        return result

    def run_job(self, job: JobView) -> str:
        self._pdf_cache.clear()
        try:
            masters = self.gateway.get_masters()
            self._check_guard(job)
            if not self.store.has_mails(job.id):
                pdf_file_id = self._pdf_stage(job, masters)
                self.store.add_mails(job.id, self._build_mails(job, masters), pdf_file_id, self.now())
            errors = self._send_stage(job, masters)
        except SkipJob as skip:
            log.info("job %s skipped: %s", job.id, skip)
            self.store.mark_unsent_skipped(job.id, str(skip), self.now())
            self.store.update_job(job.id, self.now(), state="skipped", finished_at=self.now(), last_error=str(skip)[:1000])
            return "skipped"
        except Exception as error:  # PDF・ドライブ・組み立ての失敗も送り直しの対象
            log.exception("job %s (%s %s) failed", job.id, job.kind, job.request_id)
            errors = [f"{type(error).__name__}: {error}"]
        if not errors:
            self.store.update_job(job.id, self.now(), state="done", finished_at=self.now(), last_error="")
            return "done"
        message = " / ".join(errors)[:1000]
        if job.attempts < MAX_ATTEMPTS:
            self.store.update_job(job.id, self.now(), state="pending",
                                  run_after=self.now() + RETRY_DELAYS[job.attempts - 1], last_error=message)
            return "retry"
        self.store.update_job(job.id, self.now(), state="failed", finished_at=self.now(), last_error=message)
        self._notify_admins(job, message)
        return "failed"

    # --- 照合 -------------------------------------------------------------
    def _check_guard(self, job: JobView) -> None:
        if job.kind in UNGUARDED_KINDS or not job.guard_status:
            return
        current = self.gateway.get_request(job.request_id)
        if not current or current.get("status") != job.guard_status or current.get("currentStep") != job.guard_step:
            now_state = f"{current.get('status')}/{current.get('currentStep')}" if current else "(none)"
            raise SkipJob(f"state changed: expected {job.guard_status}/{job.guard_step} now {now_state}")

    # --- ① PDF ------------------------------------------------------------
    def _needs_pdf(self, job: JobView) -> bool:
        a = job.args
        if job.kind in ("completeAndNotify_", "regeneratePdf"):
            return True
        if job.kind == "dispatchApprovalNotifications_":
            return _arg(a, 2) in (STEPS.DONE, STEPS.PURCHASING_QUOTE, STEPS.PURCHASING)
        if job.kind == "regeneratePdfAfterSplit_":
            # 遷移先で作る場合は二重に作らない。PDF をまだ持たない申請（上席の段）は作らない。
            will = _arg(a, 1) in (STEPS.PURCHASING_QUOTE, STEPS.PURCHASING, STEPS.DONE)
            return not will and bool((_arg(a, 0, {}) or {}).get("pdfFileId"))
        return False

    def _actor(self, job: JobView) -> Mapping | None:
        idx = {"dispatchApprovalNotifications_": 4, "completeAndNotify_": 1, "regeneratePdfAfterSplit_": 2,
               "regeneratePdf": 1}.get(job.kind)
        return _arg(job.args, idx) if idx is not None else None

    def _pdf_stage(self, job: JobView, masters: Masters) -> str:
        if job.kind == "trashPdf":
            # 照合なし。safety を通る（staging では staging の置き場の外＝本番の PDF は消さない）。
            self.storage.trash(masters.settings, js_str(_arg(job.args, 0)))
            return ""
        if not self._needs_pdf(job):
            return ""
        return self.make_pdf(job.request_id, self._actor(job), masters)

    def make_pdf(self, request_id: str, actor: Mapping | None, masters: Masters | None = None) -> str:
        """PDF を作ってドライブへ保存し、申請に記録する（GAS 版 createRequestPdfInternal_）。戻り値はファイル ID。

        作成中に取消されたら作った PDF をゴミ箱へ入れて SkipJob（メールも出さない）。
        手動の「PDF再作成」（generateRequestPdf）からもこれを呼んでよい。
        """
        masters = masters or self.gateway.get_masters()
        req = self.gateway.get_request(request_id)
        if not req:
            raise SkipJob("request not found")
        view = build_pdf_view(req, self.gateway.get_items(request_id), self.gateway.get_history(request_id),
                              masters, self.config.public_origin)
        data = self.renderer.render(render_pdf_html(view))
        saved = self.storage.save(masters.settings, req.get("requestDate"), pdf_file_name(req), data)
        result = self.gateway.record_pdf(request_id, saved.id, saved.url, actor)
        if not result.stored:
            self.storage.trash(masters.settings, saved.id)  # 取消済み（却下など）の申請には申請書を残さない
            raise SkipJob("request cancelled while making PDF")
        # 1 申請 = 1 ファイル。前の PDF と、作っている間に別に書かれた PDF はゴミ箱へ。
        for old in {js_str(req.get("pdfFileId")), result.replaced_file_id} - {"", saved.id}:
            self.storage.trash(masters.settings, old)
        self._pdf_cache[saved.id] = data
        return saved.id

    # --- ② メールの組み立て（GAS 版の各関数と同じ） --------------------------
    def _current(self, job: JobView) -> dict:
        """PDF を作った後の最新の行（GAS 版も PDF の後に getRequestById_ で読み直している）。"""
        current = self.gateway.get_request(job.request_id)
        if not current:
            raise SkipJob("request not found")
        return current

    def _build_mails(self, job: JobView, masters: Masters) -> list[OutgoingMail]:
        b = MailBuilder(masters, self.config.public_origin)
        a, k = job.args, job.kind
        req = _arg(a, 0, {}) if isinstance(_arg(a, 0), Mapping) else {}
        if k == "sendApprovalRequestEmail_":
            return b.approval_request(req, _arg(a, 1), _arg(a, 2, "") or "", _arg(a, 3, "") or "")
        if k == "sendRequestUpdatedEmail_":
            return b.request_updated(req, _arg(a, 1, "") or "", _arg(a, 2, "") or "", _arg(a, 3, "") or "")
        if k == "sendRejectedEmail_":
            return b.rejected(req, _arg(a, 1, "") or "")
        if k == "sendReturnedEmail_":
            return b.returned(req, _arg(a, 1, "") or "")
        if k == "sendReturnedBroadcast_":
            return b.returned_broadcast(req, self.gateway.get_items(js_str(req.get("requestId"))),
                                        _arg(a, 1, "") or "", _arg(a, 2, "") or "", _arg(a, 3, "") or "")
        if k == "notifyGeneralAffairs_":
            subject = _arg(a, 1, "") or ""
            return b.general_affairs(_ga_kind(subject), req, subject, _arg(a, 2, "") or "", attach_pdf=False)
        if k == "notifySplitReturned_":
            new_id_ = js_str(_arg(a, 0))
            new_req = self.gateway.get_request(new_id_)
            if not new_req:
                return []
            return b.split_returned(new_req, self.gateway.get_items(new_id_), _arg(a, 1, "") or "",
                                    _arg(a, 2, "") or "", _arg(a, 3, "") or "")
        if k == "dispatchApprovalNotifications_":
            next_step = _arg(a, 2, "") or ""
            if next_step == STEPS.DONE:
                return b.arranged_and_completed(self._current(job))
            if next_step == STEPS.PURCHASING_QUOTE:
                return b.quote_request(self._current(job))
            if next_step == STEPS.PURCHASING:
                return b.purchasing_request(self._current(job))
            return b.approval_requests_for_step(req, next_step, _arg(a, 3))
        if k == "completeAndNotify_":
            return b.arranged_and_completed(self._current(job))
        if k in ("regeneratePdfAfterSplit_", "regeneratePdf", "trashPdf"):
            return []
        raise ValueError(f"unknown job kind: {k}")

    # --- ③ 送信 -----------------------------------------------------------
    def _attachment(self, row: dict) -> tuple[str, bytes] | None:
        fid = row["attachment_file_id"]
        if not fid:
            return None
        data = self._pdf_cache.get(fid)
        if data is None:  # 送り直し: ドライブから読み直す
            data = self.storage.download(fid)
            self._pdf_cache[fid] = data
        req = self.gateway.get_request(row["request_id"]) or {"requestId": row["request_id"]}
        return (pdf_file_name(req), data)

    def _send_stage(self, job: JobView, masters: Masters) -> list[str]:
        errors: list[str] = []
        redirect_raw = masters.settings.get(SETTINGS_REDIRECT_KEY)
        for row in self.store.unsent_mails(job.id):
            mail = OutgoingMail(kind=row["kind"], to=[x.strip() for x in (row["to"] or "").split(",") if x.strip()],
                                cc=[x.strip() for x in (row["cc"] or "").split(",") if x.strip()], subject=row["subject"],
                                body=row["body"], attach_pdf=bool(row["attachment_file_id"]), request_id=row["request_id"])
            try:
                redirect = effective_redirect(self.config, redirect_raw, self.now())
                message_id = send_mail(mail, self.transport, self.config, redirect_raw, self._attachment(row), self.now())
                self.store.update_mail(row["id"], self.now(), sent=True, gmail_message_id=message_id, sent_at=self.now(),
                                       attempts=row["attempts"] + 1, last_error="",
                                       redirected_to=redirect.email if redirect else "")
            except Exception as error:
                log.warning("mail %s failed: %s", row["id"], error)
                msg = f"{type(error).__name__}: {error}"[:1000]
                self.store.update_mail(row["id"], self.now(), attempts=row["attempts"] + 1, last_error=msg)
                errors.append(msg)
        return errors

    def _notify_admins(self, job: JobView, message: str) -> None:
        try:
            masters = self.gateway.get_masters()
            redirect_raw = masters.settings.get(SETTINGS_REDIRECT_KEY)
            for mail in job_failure_admin_mail(masters.settings, JOB_LABELS.get(job.kind, job.kind) + "（" + job.kind + "）",
                                               job.request_id, message, self.extra_admins):
                try:
                    redirect = effective_redirect(self.config, redirect_raw, self.now())
                    mid = send_mail(mail, self.transport, self.config, redirect_raw, None, self.now())
                    self.store.log_direct_mail(mail, True, self.now(), message_id=mid,
                                               redirected_to=redirect.email if redirect else "")
                except Exception as error:  # メール自体が原因なら届かないので記録だけ残す
                    log.error("admin notification failed: %s", error)
                    self.store.log_direct_mail(mail, False, self.now(), error=str(error)[:1000])
        except Exception as error:
            log.error("admin notification failed: %s", error)
