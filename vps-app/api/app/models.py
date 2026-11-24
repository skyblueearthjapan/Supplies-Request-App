"""DB の表。シートと 1 対 1 の 8 表（列は Constants.gs の *_COLUMNS と同じ意味）と、VPS 版で増えた表。

- 列名は snake_case。JSON のキー（camelCase）への変換は serialize.py。
- 文字の列は NOT NULL、空は ''（GAS 版のセルの空と同じ扱い）。日時・日付の空は NULL。
- enum（日本語の値）は CHECK 制約で守る。
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import (
    ACTION_VALUES,
    CATEGORY_VALUES,
    JOB_STATES,
    RECIPIENT_TYPE_VALUES,
    SEND_AS_VALUES,
    STATUS_VALUES,
    STEP_VALUES,
)
from app.db import Base, UTCDateTime

# 自動で増える整数の主キー（PostgreSQL は BIGINT、SQLite は INTEGER でないと自動採番されない）
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
# 金額・数量（数量の小数を許す。単価・金額は税抜）
Num = Numeric(18, 4)


def _in(column: str, values: tuple[str, ...], allow_empty: bool = False) -> str:
    allowed = list(values) + ([""] if allow_empty else [])
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in allowed)
    return f"{column} IN ({quoted})"


def _text(**kw):
    return mapped_column(Text, nullable=False, default="", server_default="", **kw)


def _str(length: int, **kw):
    return mapped_column(String(length), nullable=False, default="", server_default="", **kw)


# ===== シートと 1 対 1 の表 =====


class Request(Base):
    """申請（REQUEST_COLUMNS）。"""

    __tablename__ = "requests"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    applicant_email: Mapped[str] = _str(256)
    applicant_name: Mapped[str] = _text()
    department: Mapped[str] = _text()
    request_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason_code: Mapped[str] = _str(16)
    reason_detail: Mapped[str] = _text()
    total_amount: Mapped[Decimal] = mapped_column(Num, nullable=False, default=0, server_default="0")
    status: Mapped[str] = _str(16)
    current_step: Mapped[str] = _str(16)
    current_approver_email: Mapped[str] = _text()  # 複数人は ", " 連結（GAS 版と同じ）
    current_approver_name: Mapped[str] = _text()
    route_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    pdf_file_id: Mapped[str] = _str(256)
    pdf_url: Mapped[str] = _text()
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0", server_default="1.0.0")
    amount_waived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    category: Mapped[str] = _str(16)
    split_from_request_id: Mapped[str] = _str(64)
    # VPS 版で追加: 更新回数（画面を開いた後の変更を見つける）
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        CheckConstraint(_in("status", STATUS_VALUES), name="ck_requests_status"),
        CheckConstraint(_in("current_step", STEP_VALUES, allow_empty=True), name="ck_requests_current_step"),
        CheckConstraint(_in("category", CATEGORY_VALUES, allow_empty=True), name="ck_requests_category"),
        Index("ix_requests_status_step", "status", "current_step"),
        Index("ix_requests_applicant_email", "applicant_email"),
        Index("ix_requests_updated_at", "updated_at"),
    )


class RequestItem(Base):
    """申請明細（ITEM_COLUMNS）。item_id は作り直しても変えない（W の約束）。"""

    __tablename__ = "request_items"

    item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("requests.request_id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    name: Mapped[str] = _text()
    model: Mapped[str] = _text()
    maker: Mapped[str] = _text()
    quantity: Mapped[Decimal | None] = mapped_column(Num, nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Num, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Num, nullable=True)
    desired_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = _text()
    unit: Mapped[str] = _str(20)


class ApproverRule(Base):
    """承認者マスタ（APPROVER_COLUMNS）。行の順番に意味があるので sort_order を持つ。"""

    __tablename__ = "approver_rules"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    department: Mapped[str] = _text()
    applicant_email: Mapped[str] = _text()
    applicant_name: Mapped[str] = _text()
    supervisor_email: Mapped[str] = _text()
    supervisor_name: Mapped[str] = _text()
    general_manager_email: Mapped[str] = _text()
    general_manager_name: Mapped[str] = _text()
    president_email: Mapped[str] = _text()
    president_name: Mapped[str] = _text()
    purchasing_email: Mapped[str] = _text()
    purchasing_name: Mapped[str] = _text()
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    supervisor_title: Mapped[str] = _text()
    general_manager_title: Mapped[str] = _text()
    president_title: Mapped[str] = _text()
    purchasing_title: Mapped[str] = _text()
    purchasing_elec_email: Mapped[str] = _text()
    purchasing_elec_name: Mapped[str] = _text()
    purchasing_elec_title: Mapped[str] = _text()


class History(Base):
    """履歴（HISTORY_COLUMNS）。seq は同じ秒の中の順番（追加した順）。"""

    __tablename__ = "history"

    seq: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    happened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    actor_email: Mapped[str] = _str(256)
    actor_name: Mapped[str] = _text()
    action: Mapped[str] = _str(32)
    from_status: Mapped[str] = _str(16)
    to_status: Mapped[str] = _str(16)
    from_step: Mapped[str] = _str(16)
    to_step: Mapped[str] = _str(16)
    comment: Mapped[str] = _text()

    __table_args__ = (
        CheckConstraint(_in("action", ACTION_VALUES), name="ck_history_action"),
        CheckConstraint(_in("from_status", STATUS_VALUES, allow_empty=True), name="ck_history_from_status"),
        CheckConstraint(_in("to_status", STATUS_VALUES, allow_empty=True), name="ck_history_to_status"),
        CheckConstraint(_in("from_step", STEP_VALUES, allow_empty=True), name="ck_history_from_step"),
        CheckConstraint(_in("to_step", STEP_VALUES, allow_empty=True), name="ck_history_to_step"),
    )


class Setting(Base):
    """設定（SETTING_COLUMNS）。値は GAS 版と同じく文字列で持つ。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = _text()
    description: Mapped[str] = _text()
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Worker(Base):
    """作業員マスタ（WORKER_CACHE_COLUMNS）。外部シートから同期する。"""

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    worker_code: Mapped[str] = _text()
    name: Mapped[str] = _text()
    dept: Mapped[str] = _text()
    location: Mapped[str] = _text()
    staff_type: Mapped[str] = _text()


class NotificationRecipient(Base):
    """通知宛先マスタ（RECIPIENT_COLUMNS）。"""

    __tablename__ = "notification_recipients"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    type: Mapped[str] = _str(16)
    email: Mapped[str] = _str(256)
    name: Mapped[str] = _text()
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    send_as: Mapped[str] = mapped_column(String(4), nullable=False, default="TO", server_default="TO")

    __table_args__ = (
        CheckConstraint(_in("type", RECIPIENT_TYPE_VALUES), name="ck_recipients_type"),
        CheckConstraint(_in("send_as", SEND_AS_VALUES), name="ck_recipients_send_as"),
    )


class DeptSupervisor(Base):
    """部署別上席マスタ（DEPT_SUPERVISOR_COLUMNS）。DeptApprovers シートから同期する。"""

    __tablename__ = "dept_supervisors"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    department: Mapped[str] = _text()
    email: Mapped[str] = _str(256)
    name: Mapped[str] = _text()
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    title: Mapped[str] = _text()
    notify_email: Mapped[str] = _text()


# ===== VPS 版で増えた表 =====


class Job(Base):
    """後回しの仕事（outbox）。状態の変更と同じトランザクションで積む。

    guard_*: 積んだ時点の申請の状態と段。実行時に変わっていたら捨てる（Jobs.gs の guard と同じ）。
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    args_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    request_id: Mapped[str] = _str(64)
    guard_status: Mapped[str] = _str(16)
    guard_step: Mapped[str] = _str(16)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str] = _text()
    # この仕事で作った PDF（ドライブのファイル ID）。PDF の後で失敗したとき、送り直しで作り直さないため
    pdf_file_id: Mapped[str] = _str(256)
    run_after: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(_in("state", JOB_STATES), name="ck_jobs_state"),
        Index("ix_jobs_state_run_after", "state", "run_after"),
        Index("ix_jobs_request_id_state", "request_id", "state"),
    )


class MailLog(Base):
    """メール 1 通ずつの記録。送り直しは sent=false のものだけ（決定事項 9-2）。"""

    __tablename__ = "mail_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    request_id: Mapped[str] = _str(64)
    kind: Mapped[str] = _str(64)
    to_addrs: Mapped[str] = _text()  # 本来の宛先（", " 連結）
    cc_addrs: Mapped[str] = _text()
    subject: Mapped[str] = _text()
    body_html: Mapped[str] = _text()
    body_text: Mapped[str] = _text()
    attachment_file_id: Mapped[str] = _str(256)  # 添付する PDF（ドライブのファイル ID）
    redirected_to: Mapped[str] = _str(256)  # テスト転送で実際に送った先（転送なしは ''）
    sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    gmail_message_id: Mapped[str] = _str(128)
    # 送る前に決めた Message-ID。時間切れなどの後は、これで送信済みかを確かめてから送り直す
    message_id_header: Mapped[str] = _str(256)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str] = _text()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_mail_log_sent_created", "sent", "created_at"),)


class GoogleCredential(Base):
    """委任トークン（imaizumi@ の Gmail・Drive・Sheets の権限）。リフレッシュトークンは Fernet で暗号化。"""

    __tablename__ = "google_credentials"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)  # 'delegate'
    account_email: Mapped[str] = _str(256)  # 委任したアカウント
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = _text()  # 空白区切り
    granted_by_email: Mapped[str] = _str(256)  # 許可の操作をした管理者
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class IdempotencyKey(Base):
    """二重押し対策。同じ人の同じキーの 2 回目は、1 回目の結果を返す。"""

    __tablename__ = "idempotency_keys"

    user_email: Mapped[str] = mapped_column(String(256), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fn: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = _text()
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class UserSession(Base):
    """ログインのセッション。クッキーには署名付きの乱数を入れ、DB には その SHA-256 だけを置く。"""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    name: Mapped[str] = _text()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)


class SyncRun(Base):
    """定期処理（マスタ同期・スプレッドシート書き出し・バックアップ確認など）の結果。管理画面に最終日時を出す。"""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)  # 例 'workers' / 'dept_supervisors' / 'sheets_export'
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    message: Mapped[str] = _text()

    __table_args__ = (Index("ix_sync_runs_kind_started", "kind", "started_at"),)


__all__ = [
    "ApproverRule",
    "DeptSupervisor",
    "GoogleCredential",
    "History",
    "IdempotencyKey",
    "Job",
    "MailLog",
    "NotificationRecipient",
    "Request",
    "RequestItem",
    "Setting",
    "SyncRun",
    "UserSession",
    "Worker",
]
