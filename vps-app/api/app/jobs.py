"""後回しの仕事（outbox）: PDF 作成とメール送信。GAS 版 Jobs.gs と Workflow.gs の dispatch 部分の移植。

GAS 版からの変更（計画書 9-2・実装仕様 3 章）:
- 仕事は状態の変更と同じトランザクションで jobs 表に積む（enqueue_job を W のトランザクションの中で呼ぶ）。
- メールは mail_log に 1 通ずつ記録し、送れなかった分だけを 5 分・30 分・2 時間後に送り直す（二重には届かない）。
  それでも失敗したら管理者へ知らせる。
- 実行のたびに申請の状態と段を照合し、変わっていれば出さずに捨てる（差戻し共有と分割の通知は照合しない）。
- 同じ申請の仕事は同時に 1 本だけ動かす（同じ申請の PDF を 2 本同時に作らない）。
- 取り出しは SELECT ... FOR UPDATE SKIP LOCKED（SQLite では無視される）。

段の分け方: ①PDF（要る仕事だけ。作った ID を jobs に残すので送り直しで作り直さない）
           ②メールを組み立てて mail_log に積む（1 回だけ）
           ③mail_log の未送信分を送る
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from sqlalchemy import and_, func, or_, select

from .services.drive import PdfStorage
from .services.mail import MailTransport, effective_redirect, send_mail
from .services.mailpdf_common import STATUS, STEPS, MailPdfConfig, Masters, js_str
from .services.pdf import PdfRenderer, build_pdf_view, pdf_file_name, render_pdf_html
from .services.recipients import MailBuilder, OutgoingMail, job_failure_admin_mail

log = logging.getLogger(__name__)

RETRY_DELAYS = (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1  # 最初の 1 回 + 送り直し 3 回
RUNNING_LEASE = timedelta(minutes=15)  # これより長く running のままなら止まったとみなして拾い直す
SETTINGS_REDIRECT_KEY = "mailRedirectTo"  # 設定に置くテスト転送の値（email|失効エポックミリ秒）


class JobKind:
    APPROVAL_REQUEST = "approval_request"  # {request, approver | approvers[], extraNote?, subjectPrefix?}
    RECALL_REQUESTS = "recall_requests"  # {request, step}  recallStep の再承認依頼
    REQUEST_UPDATED = "request_updated"  # {request, changeSummary, editorName, editorEmail}
    RETURNED = "returned"  # {request, comment}
    REJECTED = "rejected"  # {request, comment}
    RETURNED_BROADCAST = "returned_broadcast"  # {request(差戻し前), stepAtReturn, comment, actorName}  照合しない
    SPLIT_RETURNED = "split_returned"  # {newRequestId, stepAtSplit, comment, actorName}  照合しない
    QUOTE_CONFIRMED = "quote_confirmed"  # {request, gm, amountSummary, total, threshold, over}
    EXPEDITED = "expedited"  # {request, gm, comment}
    APPROVAL_DISPATCH = "approval_dispatch"  # {request, curKey, nextStep, nextApprover, actor}
    COMPLETE = "complete"  # {request, actor}  手配完了（arrangeComplete）
    REGENERATE_PDF_AFTER_SPLIT = "regenerate_pdf_after_split"  # {request, nextStep, actor}
    PDF = "pdf"  # {actor}  手動の PDF 再作成


UNGUARDED_KINDS = {JobKind.RETURNED_BROADCAST, JobKind.SPLIT_RETURNED}
ALL_KINDS = {v for k, v in vars(JobKind).items() if not k.startswith("_")}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)  # SQLite は時差を落とすので UTC とみなす
    return value


# ---------------------------------------------------------------------------
# 申請・マスタの読み書き（担当 W / F が DB で実装する。テストは test 側の偽物）
# ---------------------------------------------------------------------------

@dataclass
class RecordPdfResult:
    stored: bool  # False: 取消済み・無い申請だったので書かなかった（作った PDF は捨てる）
    replaced_file_id: str = ""  # 書いた時点で申請が指していた PDF（作り始めた後に別の PDF ができていたとき用）


class RequestGateway(Protocol):
    def get_request(self, request_id: str) -> dict | None:
        """申請 1 件（シートの列名の camelCase。日時は文字列か datetime）。"""

    def get_items(self, request_id: str) -> list[dict]:
        """明細（行番号順）。"""

    def get_history(self, request_id: str) -> list[dict]:
        """履歴（発生日時・seq の順）。"""

    def get_masters(self) -> Masters:
        """承認者マスタ・上席マスタ・通知宛先マスタ・設定（行の順番どおり）。"""

    def record_pdf(self, request_id: str, file_id: str, url: str, actor: Mapping | None) -> RecordPdfResult:
        """GAS 版 writePdfResult_ の DB 部分。1 トランザクションで:
        1. 申請行を SELECT ... FOR UPDATE で読み直す
        2. 無い・取消（STATUS.CANCELLED）なら何も書かず stored=False
        3. replaced_file_id = 今の pdfFileId（新しい file_id と同じなら ''）
        4. updatedAt=今, pdfFileId=file_id, pdfUrl=url を書く（row_version も上げる）
        5. 履歴 PDF作成（操作者=actor、状態・段は今の値のまま、コメント=url）を足す
        """


# ---------------------------------------------------------------------------
# jobs / mail_log の出し入れ（モデルは担当 F。列名は下の docstring のとおり）
# ---------------------------------------------------------------------------

@dataclass
class JobView:
    id: int
    kind: str
    request_id: str
    payload: dict
    guard_status: str | None
    guard_step: str | None
    attempts: int
    pdf_file_id: str | None
    pdf_url: str | None
    mails_built: bool


class SqlJobStore:
    """jobs / mail_log を SQLAlchemy で扱う。

    Job モデルに要る列: id(int PK), kind(str), request_id(str), payload_json(Text), guard_status(str|None),
      guard_step(str|None), state(str: pending|running|done|skipped|failed|cleared), attempts(int),
      run_after(timestamptz), started_at(timestamptz|None), finished_at(timestamptz|None), last_error(Text|None),
      pdf_file_id(str|None), pdf_url(str|None), mails_built(bool), created_at(timestamptz)
    MailLog モデルに要る列: id(int PK), job_id(int|None FK jobs.id), request_id(str), kind(str),
      to_addrs(Text), cc_addrs(Text), subject(Text), body(Text), attach_pdf(bool), pdf_file_id(str|None),
      attachment_name(str|None), state(str: pending|sent|error|skipped), attempts(int),
      gmail_message_id(str|None), redirected_to(str|None), last_error(Text|None), created_at, sent_at(timestamptz|None)
    """

    def __init__(self, session_factory: Callable[[], Any], job_model: Any, mail_log_model: Any):
        self.session_factory = session_factory
        self.Job = job_model
        self.MailLog = mail_log_model

    # --- 積む（W が状態の変更と同じトランザクションで呼ぶ） -------------------
    def enqueue(self, session: Any, kind: str, request_id: str, payload: Mapping[str, Any] | None = None,
                guard: Mapping[str, Any] | None = None, now: datetime | None = None) -> Any:
        """仕事を 1 件積む。guard は積んだ時点の申請 {status, currentStep}（照合しない仕事は None）。"""
        if kind not in ALL_KINDS:
            raise ValueError(f"後回しにできない仕事です: {kind}")
        now = now or utcnow()
        guarded = kind not in UNGUARDED_KINDS and guard is not None
        job = self.Job(
            kind=kind,
            request_id=request_id,
            payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
            guard_status=guard.get("status") if guarded else None,
            guard_step=guard.get("currentStep") if guarded else None,
            state="pending",
            attempts=0,
            run_after=now,
            mails_built=False,
            created_at=now,
        )
        session.add(job)
        return job

    def count_pending(self, session: Any, request_id: str) -> int:
        """詳細画面の pendingJobs（まだ終わっていない仕事の数）。"""
        J = self.Job
        return session.scalar(select(func.count()).select_from(J).where(
            J.request_id == request_id, J.state.in_(("pending", "running")))) or 0

    # --- 取り出し ---------------------------------------------------------
    def claim(self, now: datetime | None = None, limit: int = 10) -> list[JobView]:
        now = now or utcnow()
        J = self.Job
        stale = now - RUNNING_LEASE
        claimed: list[JobView] = []
        with self.session_factory() as s, s.begin():
            due = or_(and_(J.state == "pending", J.run_after <= now), and_(J.state == "running", J.started_at < stale))
            candidates = s.scalars(select(J).where(due).order_by(J.id).limit(limit * 3)
                                   .with_for_update(skip_locked=True)).all()
            taken_requests: set[str] = set()
            for job in candidates:
                if len(claimed) >= limit:
                    break
                if job.request_id in taken_requests:
                    continue
                # 同じ申請の、先に積まれて今すぐ動かす仕事か、動いている仕事があれば後回し（同時に 1 本）。
                busy = s.scalar(select(func.count()).select_from(J).where(
                    J.request_id == job.request_id, J.id != job.id,
                    or_(and_(J.state == "running", J.started_at >= stale),
                        and_(J.state == "pending", J.run_after <= now, J.id < job.id)))) or 0
                if busy:
                    continue
                job.state = "running"
                job.started_at = now
                job.attempts = (job.attempts or 0) + 1
                taken_requests.add(job.request_id)
                claimed.append(self._view(job))
        return claimed

    @staticmethod
    def _view(job: Any) -> JobView:
        try:
            payload = json.loads(job.payload_json or "{}")
        except ValueError:
            payload = {}
        return JobView(id=job.id, kind=job.kind, request_id=job.request_id, payload=payload,
                       guard_status=job.guard_status, guard_step=job.guard_step, attempts=job.attempts or 0,
                       pdf_file_id=job.pdf_file_id, pdf_url=job.pdf_url, mails_built=bool(job.mails_built))

    def update_job(self, job_id: int, **fields: Any) -> None:
        with self.session_factory() as s, s.begin():
            job = s.get(self.Job, job_id)
            for k, v in fields.items():
                setattr(job, k, v)

    # --- mail_log ---------------------------------------------------------
    def add_mails(self, job_id: int, mails: Sequence[OutgoingMail], pdf_file_id: str | None, attachment_name: str | None,
                  now: datetime | None = None) -> None:
        """組み立てたメールを積み、mails_built を立てる（1 トランザクション。2 回は積まない）。"""
        now = now or utcnow()
        with self.session_factory() as s, s.begin():
            job = s.get(self.Job, job_id)
            if job.mails_built:
                return
            for m in mails:
                s.add(self.MailLog(job_id=job_id, request_id=m.request_id or job.request_id, kind=m.kind,
                                   to_addrs=m.to_header(), cc_addrs=m.cc_header(), subject=m.subject, body=m.body,
                                   attach_pdf=bool(m.attach_pdf), pdf_file_id=pdf_file_id if m.attach_pdf else None,
                                   attachment_name=attachment_name if m.attach_pdf else None,
                                   state="pending", attempts=0, created_at=now))
            job.mails_built = True

    def unsent_mails(self, job_id: int) -> list[dict]:
        M = self.MailLog
        with self.session_factory() as s:
            rows = s.scalars(select(M).where(M.job_id == job_id, M.state.in_(("pending", "error"))).order_by(M.id)).all()
            return [{"id": r.id, "kind": r.kind, "request_id": r.request_id, "to": r.to_addrs, "cc": r.cc_addrs,
                     "subject": r.subject, "body": r.body, "attach_pdf": bool(r.attach_pdf), "pdf_file_id": r.pdf_file_id,
                     "attachment_name": r.attachment_name, "attempts": r.attempts or 0} for r in rows]

    def update_mail(self, mail_id: int, **fields: Any) -> None:
        with self.session_factory() as s, s.begin():
            row = s.get(self.MailLog, mail_id)
            for k, v in fields.items():
                setattr(row, k, v)

    def skip_unsent_mails(self, job_id: int, reason: str) -> None:
        M = self.MailLog
        with self.session_factory() as s, s.begin():
            for r in s.scalars(select(M).where(M.job_id == job_id, M.state.in_(("pending", "error")))).all():
                r.state = "skipped"
                r.last_error = reason

    def log_direct_mail(self, mail: OutgoingMail, state: str, message_id: str = "", error: str = "",
                        now: datetime | None = None) -> None:
        now = now or utcnow()
        with self.session_factory() as s, s.begin():
            s.add(self.MailLog(job_id=None, request_id=mail.request_id, kind=mail.kind, to_addrs=mail.to_header(),
                               cc_addrs=mail.cc_header(), subject=mail.subject, body=mail.body, attach_pdf=False,
                               state=state, attempts=1, gmail_message_id=message_id or None,
                               last_error=error or None, created_at=now, sent_at=now if state == "sent" else None))

    # --- 管理画面（listFailedJobs / clearFailedJobs の代わり） --------------
    def list_failed(self) -> list[dict]:
        J = self.Job
        with self.session_factory() as s:
            rows = s.scalars(select(J).where(J.state == "failed").order_by(J.id)).all()
            return [{"id": r.id, "kind": r.kind, "requestId": r.request_id, "error": r.last_error or "",
                     "attempts": r.attempts, "failedAt": r.finished_at} for r in rows]

    def clear_failed(self) -> int:
        J = self.Job
        with self.session_factory() as s, s.begin():
            rows = s.scalars(select(J).where(J.state == "failed")).all()
            for r in rows:
                r.state = "cleared"
            return len(rows)


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

class SkipJob(Exception):
    """状態が変わった・取消された等で、出さずに捨てる。"""


@dataclass
class JobRunner:
    store: SqlJobStore
    gateway: RequestGateway
    transport: MailTransport
    storage: PdfStorage
    renderer: PdfRenderer
    config: MailPdfConfig
    extra_admins: Sequence[str] = ()  # BOOTSTRAP_ADMINS
    now: Callable[[], datetime] = utcnow
    _pdf_cache: dict[str, bytes] = field(default_factory=dict)

    # --- 1 回分 -----------------------------------------------------------
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
            pdf_file_id, attachment_name = self._pdf_stage(job, masters)
            if not job.mails_built:
                mails = self._build_mails(job, masters)
                self.store.add_mails(job.id, mails, pdf_file_id, attachment_name, self.now())
            errors = self._send_stage(job, masters)
        except SkipJob as skip:
            log.info("job %s skipped: %s", job.id, skip)
            self.store.skip_unsent_mails(job.id, str(skip))
            self.store.update_job(job.id, state="skipped", finished_at=self.now(), last_error=str(skip)[:1000])
            return "skipped"
        except Exception as error:  # PDF・ドライブ・組み立ての失敗も送り直しの対象
            log.exception("job %s (%s %s) failed", job.id, job.kind, job.request_id)
            errors = [f"{type(error).__name__}: {error}"]
        if not errors:
            self.store.update_job(job.id, state="done", finished_at=self.now(), last_error=None)
            return "done"
        message = " / ".join(errors)[:1000]
        if job.attempts < MAX_ATTEMPTS:
            self.store.update_job(job.id, state="pending", run_after=self.now() + RETRY_DELAYS[job.attempts - 1],
                                  last_error=message)
            return "retry"
        self.store.update_job(job.id, state="failed", finished_at=self.now(), last_error=message)
        self._notify_admins(job, message)
        return "failed"

    # --- 照合 -------------------------------------------------------------
    def _check_guard(self, job: JobView) -> None:
        if job.kind in UNGUARDED_KINDS or job.guard_status is None:
            return
        current = self.gateway.get_request(job.request_id)
        if not current or current.get("status") != job.guard_status or current.get("currentStep") != job.guard_step:
            now_state = f"{current.get('status')}/{current.get('currentStep')}" if current else "(none)"
            raise SkipJob(f"state changed: expected {job.guard_status}/{job.guard_step} now {now_state}")

    # --- ① PDF ------------------------------------------------------------
    def _needs_pdf(self, job: JobView) -> bool:
        p = job.payload
        if job.kind in (JobKind.COMPLETE, JobKind.PDF):
            return True
        if job.kind == JobKind.APPROVAL_DISPATCH:
            return p.get("nextStep") in (STEPS.DONE, STEPS.PURCHASING_QUOTE, STEPS.PURCHASING)
        if job.kind == JobKind.REGENERATE_PDF_AFTER_SPLIT:
            # 遷移先で作り直す場合は二重に作らない。PDF をまだ持たない申請（上席の段）は作らない。
            will = p.get("nextStep") in (STEPS.PURCHASING_QUOTE, STEPS.PURCHASING, STEPS.DONE)
            return not will and bool((p.get("request") or {}).get("pdfFileId"))
        return False

    def _pdf_stage(self, job: JobView, masters: Masters) -> tuple[str | None, str | None]:
        if not self._needs_pdf(job):
            return None, None
        if job.pdf_file_id:  # 送り直し: 前の回に作った PDF を使う
            req = self.gateway.get_request(job.request_id) or {}
            return job.pdf_file_id, pdf_file_name(req)
        file_id, url, name = self.make_pdf(job.request_id, job.payload.get("actor"), masters)
        self.store.update_job(job.id, pdf_file_id=file_id, pdf_url=url)
        job.pdf_file_id, job.pdf_url = file_id, url
        return file_id, name

    def make_pdf(self, request_id: str, actor: Mapping | None, masters: Masters) -> tuple[str, str, str]:
        """PDF を作ってドライブへ保存し、申請に記録する。作成中に取消されたら捨てて SkipJob。"""
        req = self.gateway.get_request(request_id)
        if not req:
            raise SkipJob("request not found")
        view = build_pdf_view(req, self.gateway.get_items(request_id), self.gateway.get_history(request_id),
                              masters, self.config.public_origin)
        data = self.renderer.render(render_pdf_html(view))
        name = pdf_file_name(req)
        saved = self.storage.save(masters.settings, req.get("requestDate"), name, data)
        result = self.gateway.record_pdf(request_id, saved.id, saved.url, actor)
        if not result.stored:
            # 取消済み（却下など）の申請には申請書を残さない。メールも出さない。
            self.storage.trash(masters.settings, saved.id)
            raise SkipJob("request cancelled while making PDF")
        # 1 申請 = 1 ファイル。前の PDF と、作っている間に別に書かれた PDF はゴミ箱へ。
        for old in {js_str(req.get("pdfFileId")), result.replaced_file_id} - {"", saved.id}:
            self.storage.trash(masters.settings, old)
        self._pdf_cache[saved.id] = data
        return saved.id, saved.url, name

    # --- ② メールの組み立て ------------------------------------------------
    def _snapshot(self, job: JobView) -> dict:
        snap = job.payload.get("request")
        if snap:
            return dict(snap)
        current = self.gateway.get_request(job.request_id)
        if not current:
            raise SkipJob("request not found")
        return current

    def _build_mails(self, job: JobView, masters: Masters) -> list[OutgoingMail]:
        b = MailBuilder(masters, self.config.public_origin)
        p = job.payload
        k = job.kind
        if k == JobKind.APPROVAL_REQUEST:
            req = self._snapshot(job)
            approvers = p.get("approvers") or ([p["approver"]] if p.get("approver") else [])
            out: list[OutgoingMail] = []
            for a in approvers:
                out += b.approval_request(req, a, p.get("extraNote") or "", p.get("subjectPrefix") or "")
            return out
        if k == JobKind.RECALL_REQUESTS:
            return b.recall_approval_requests(self._snapshot(job), p.get("step") or "")
        if k == JobKind.REQUEST_UPDATED:
            return b.request_updated(self._snapshot(job), p.get("changeSummary") or "", p.get("editorName") or "",
                                     p.get("editorEmail") or "")
        if k == JobKind.RETURNED:
            return b.returned(self._snapshot(job), p.get("comment") or "")
        if k == JobKind.REJECTED:
            return b.rejected(self._snapshot(job), p.get("comment") or "")
        if k == JobKind.RETURNED_BROADCAST:
            req = self._snapshot(job)
            return b.returned_broadcast(req, self.gateway.get_items(job.request_id), p.get("stepAtReturn") or "",
                                        p.get("comment") or "", p.get("actorName") or "")
        if k == JobKind.SPLIT_RETURNED:
            new_id = p.get("newRequestId") or job.request_id
            new_req = self.gateway.get_request(new_id)
            if not new_req:
                return []
            return b.split_returned(new_req, self.gateway.get_items(new_id), p.get("stepAtSplit") or "",
                                    p.get("comment") or "", p.get("actorName") or "")
        if k == JobKind.QUOTE_CONFIRMED:
            return b.quote_confirmed(self._snapshot(job), p.get("gm") or {}, p.get("amountSummary") or "",
                                     p.get("total"), p.get("threshold"), bool(p.get("over")))
        if k == JobKind.EXPEDITED:
            return b.expedited(self._snapshot(job), p.get("gm") or {}, p.get("comment") or "")
        if k == JobKind.APPROVAL_DISPATCH:
            next_step = p.get("nextStep")
            if next_step == STEPS.DONE:
                return b.arranged_and_completed(self._current(job))
            if next_step == STEPS.PURCHASING_QUOTE:
                return b.quote_request(self._current(job))
            if next_step == STEPS.PURCHASING:
                return b.purchasing_request(self._current(job))
            return b.approval_requests_for_step(self._snapshot(job), next_step or "", p.get("nextApprover"))
        if k == JobKind.COMPLETE:
            return b.arranged_and_completed(self._current(job))
        if k in (JobKind.REGENERATE_PDF_AFTER_SPLIT, JobKind.PDF):
            return []
        raise ValueError(f"unknown job kind: {k}")

    def _current(self, job: JobView) -> dict:
        """PDF を作った後の申請（pdfUrl が入った最新の行）。GAS 版も PDF の後に読み直している。"""
        current = self.gateway.get_request(job.request_id)
        if not current:
            raise SkipJob("request not found")
        if current.get("status") == STATUS.CANCELLED:
            raise SkipJob("request cancelled")
        return current

    # --- ③ 送信 -----------------------------------------------------------
    def _attachment(self, row: dict) -> tuple[str, bytes] | None:
        if not row["attach_pdf"] or not row["pdf_file_id"]:
            return None
        data = self._pdf_cache.get(row["pdf_file_id"])
        if data is None:
            data = self.storage.download(row["pdf_file_id"])
            self._pdf_cache[row["pdf_file_id"]] = data
        return (row["attachment_name"] or "申請書.pdf", data)

    def _send_stage(self, job: JobView, masters: Masters) -> list[str]:
        errors: list[str] = []
        redirect_raw = masters.settings.get(SETTINGS_REDIRECT_KEY)
        for row in self.store.unsent_mails(job.id):
            mail = OutgoingMail(kind=row["kind"], to=[x for x in (row["to"] or "").split(",") if x],
                                cc=[x for x in (row["cc"] or "").split(",") if x], subject=row["subject"],
                                body=row["body"], attach_pdf=row["attach_pdf"], request_id=row["request_id"])
            try:
                redirect = effective_redirect(self.config, redirect_raw, self.now())
                message_id = send_mail(mail, self.transport, self.config, redirect_raw, self._attachment(row), self.now())
                self.store.update_mail(row["id"], state="sent", gmail_message_id=message_id, sent_at=self.now(),
                                       attempts=row["attempts"] + 1, last_error=None,
                                       redirected_to=redirect.email if redirect else None)
            except Exception as error:
                log.warning("mail %s failed: %s", row["id"], error)
                msg = f"{type(error).__name__}: {error}"[:1000]
                self.store.update_mail(row["id"], state="error", attempts=row["attempts"] + 1, last_error=msg)
                errors.append(msg)
        return errors

    def _notify_admins(self, job: JobView, message: str) -> None:
        try:
            masters = self.gateway.get_masters()
            for mail in job_failure_admin_mail(masters.settings, job.kind, job.request_id, message, self.extra_admins):
                try:
                    mid = send_mail(mail, self.transport, self.config, masters.settings.get(SETTINGS_REDIRECT_KEY),
                                    None, self.now())
                    self.store.log_direct_mail(mail, "sent", message_id=mid, now=self.now())
                except Exception as error:  # メール自体が原因なら届かないので記録だけ残す
                    log.error("admin notification failed: %s", error)
                    self.store.log_direct_mail(mail, "error", error=str(error)[:1000], now=self.now())
        except Exception as error:
            log.error("admin notification failed: %s", error)
