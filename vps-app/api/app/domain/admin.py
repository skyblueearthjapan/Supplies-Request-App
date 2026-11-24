"""getBootstrap と管理画面（Workflow.gs の save*・DeptSupervisors.gs / MasterData.gs の手動同期・
Email.gs のテスト転送・Jobs.gs の失敗した後処理）。

VPS 版で足したもの（画面担当 U の依頼）:
- getBootstrap に delegate・mailRedirect・syncStatus（管理者だけ。ほかの人には null）
- saveAdminAll(approvers, recipients, settings): 承認者マスタ・通知宛先・設定を 1 つのトランザクションで保存
- setMailRedirect(minutes) / clearMailRedirect(): テスト転送（宛先はログイン中の本人。期限付き）
- listFailedJobs() / retryFailedJob(jobId)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import google_delegate
from app.constants import APP_NAME, APP_VERSION, RECIPIENT_TYPE_VALUES
from app.domain import outbox, store
from app.domain.helpers import js_string, normalize_email, parse_boolean, parse_number, sanitize_text
from app.domain.labels import (
    ACTION_LABELS,
    DEFAULT_UNIT,
    REASONS,
    STATUS_LABELS,
    STEP_LABELS,
    UNIT_OPTIONS,
    UNIT_OTHER,
)
from app.domain.masters import Masters, to_client_approver
from app.domain.workflow import Op
from app.models import ApproverRule, DeptSupervisor, NotificationRecipient, SyncRun, Worker
from app.rpc import RpcContext, RpcError
from app.serialize import fmt_datetime, worker_row
from app.timeutil import JST, now_utc

MAIL_REDIRECT_KEY = "mailRedirectTo"  # 設定の表に「転送先|失効エポックミリ秒」（GAS 版のスクリプトプロパティと同じ形）
MAIL_REDIRECT_DESCRIPTION = "テスト転送（VPS 版）。転送先|失効エポックミリ秒。管理画面から設定する。"
MAIL_REDIRECT_DEFAULT_MINUTES = 120

SYNC_KIND_WORKERS = "workers"
SYNC_KIND_DEPT_SUPERVISORS = "dept_supervisors"


def assert_admin(op: Op) -> None:
    if not op.admin:
        raise RpcError("管理者のみ実行できます。")


# ===== テスト転送 =====


def get_mail_redirect(db: Session, settings) -> dict[str, Any]:
    """{active, to, until, forced}。staging は MAIL_REDIRECT_TO へ常に転送（forced）。

    担当 M（メール送信）もこの関数で転送先を決める（active なら to へ 1 通だけ送る）。
    """
    if settings.is_staging:
        return {"active": True, "to": settings.mail_redirect_to, "until": "", "forced": True}
    raw = store.load_settings(db).get(MAIL_REDIRECT_KEY, "")
    parts = js_string(raw).split("|")
    email = normalize_email(parts[0])
    until_ms = parse_number(parts[1]) if len(parts) > 1 else 0
    if email and until_ms > 0 and until_ms > time.time() * 1000:
        until = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M")
        return {"active": True, "to": email, "until": until, "forced": False}
    return {"active": False, "to": "", "until": "", "forced": False}


def set_mail_redirect(ctx: RpcContext, minutes: Any = None) -> dict[str, Any]:
    op = Op(ctx)
    assert_admin(op)
    mins = parse_number(minutes)
    mins = mins if mins > 0 else MAIL_REDIRECT_DEFAULT_MINUTES
    until_ms = int(time.time() * 1000 + mins * 60 * 1000)
    store.save_raw_setting(op.db, MAIL_REDIRECT_KEY, f"{op.email}|{until_ms}", MAIL_REDIRECT_DESCRIPTION)
    return get_mail_redirect(op.db, ctx.settings)


def clear_mail_redirect(ctx: RpcContext) -> dict[str, Any]:
    op = Op(ctx)
    assert_admin(op)
    store.save_raw_setting(op.db, MAIL_REDIRECT_KEY, "", MAIL_REDIRECT_DESCRIPTION)
    return get_mail_redirect(op.db, ctx.settings)


# ===== 同期の状態 =====


def _last_sync(db: Session, kind: str) -> dict[str, Any]:
    run = db.execute(
        select(SyncRun).where(SyncRun.kind == kind).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        return {"lastSyncedAt": "", "ok": False, "count": 0, "error": ""}
    return {
        "lastSyncedAt": fmt_datetime(run.finished_at or run.started_at),
        "ok": bool(run.ok),
        "count": int(run.row_count or 0),
        "error": "" if run.ok else (run.message or ""),
    }


def sync_status(db: Session) -> dict[str, Any]:
    return {"workers": _last_sync(db, SYNC_KIND_WORKERS), "deptSupervisors": _last_sync(db, SYNC_KIND_DEPT_SUPERVISORS)}


def delegate_status(db: Session) -> dict[str, Any]:
    status = google_delegate.get_delegate_status(db)
    return {"granted": bool(status["authorized"]), "email": status["accountEmail"], "grantedAt": status["grantedAt"]}


# ===== getBootstrap =====


def get_workers(db: Session) -> list[dict[str, str]]:
    result = []
    for w in db.execute(select(Worker).order_by(Worker.sort_order, Worker.id)).scalars():
        row = worker_row(w)
        if not row["workerCode"]:
            continue
        result.append({
            "code": sanitize_text(row["workerCode"], 100),
            "name": sanitize_text(row["name"], 100),
            "dept": sanitize_text(row["dept"], 100),
            "location": sanitize_text(row["location"], 100),
            "staffType": sanitize_text(row["staffType"], 100),
        })
    return result


def get_departments(approvers: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in approvers:
        dept = row["department"]
        if dept and dept not in seen:
            seen.append(dept)
    return seen


def build_bootstrap(op: Op) -> dict[str, Any]:
    op.db.flush()
    op.reload_masters()
    op.ctx._is_admin = None  # 設定の保存で管理者が変わったときに読み直す
    masters: Masters = op.masters
    settings = op.settings
    admin = op.admin
    approvers = masters.approvers
    return {
        "appName": APP_NAME,
        "version": APP_VERSION,
        "user": {"email": op.email, "name": op.name},
        "isAdmin": admin,
        "settings": {
            "thresholdAmount": store.threshold_amount(settings),
            "pdfFolderId": settings.get("pdfFolderId") or "",
            "adminEmails": settings.get("adminEmails") or "",
            "enableEmailNotifications": store.email_notifications_enabled(settings),
        },
        "reasons": REASONS,
        "unitOptions": UNIT_OPTIONS,
        "defaultUnit": DEFAULT_UNIT,
        "unitOther": UNIT_OTHER,
        "statusLabels": STATUS_LABELS,
        "stepLabels": STEP_LABELS,
        "actionLabels": ACTION_LABELS,
        "approvers": approvers if admin else [],
        "deptSupervisors": masters.dept_supervisors if admin else [],
        "recipients": masters.recipients if admin else [],
        "workers": get_workers(op.db),
        "departments": get_departments(approvers),
        "myApproverRule": masters.find_approver_rule(op.email, ""),
        # VPS 版で追加（管理画面用。管理者以外には null）
        "delegate": delegate_status(op.db) if admin else None,
        "mailRedirect": get_mail_redirect(op.db, op.ctx.settings) if admin else None,
        "syncStatus": sync_status(op.db) if admin else None,
    }


def get_bootstrap(ctx: RpcContext) -> dict[str, Any]:
    return build_bootstrap(Op(ctx))


# ===== 保存 =====


def _save_settings(op: Op, data: Any) -> None:
    store.save_settings(op.db, store.normalize_settings_input(data))


def _save_approvers(op: Op, rows: Any) -> None:
    normalized = [to_client_approver(r if isinstance(r, dict) else {}) for r in (rows if isinstance(rows, list) else [])]
    normalized = [r for r in normalized if r["department"] or r["applicantEmail"]]
    op.db.execute(delete(ApproverRule))
    for order, row in enumerate(normalized, start=1):
        op.db.add(ApproverRule(
            sort_order=order,
            department=row["department"],
            applicant_email=row["applicantEmail"],
            applicant_name=row["applicantName"],
            supervisor_email=row["supervisorEmail"],
            supervisor_name=row["supervisorName"],
            general_manager_email=row["generalManagerEmail"],
            general_manager_name=row["generalManagerName"],
            president_email=row["presidentEmail"],
            president_name=row["presidentName"],
            purchasing_email=row["purchasingEmail"],
            purchasing_name=row["purchasingName"],
            active=bool(row["active"]),
            supervisor_title=row["supervisorTitle"],
            general_manager_title=row["generalManagerTitle"],
            president_title=row["presidentTitle"],
            purchasing_title=row["purchasingTitle"],
            purchasing_elec_email=row["purchasingElecEmail"],
            purchasing_elec_name=row["purchasingElecName"],
            purchasing_elec_title=row["purchasingElecTitle"],
        ))
    op.db.flush()


def _save_recipients(op: Op, rows: Any) -> None:
    normalized = []
    for raw in rows if isinstance(rows, list) else []:
        data = raw if isinstance(raw, dict) else {}
        type_ = sanitize_text(data.get("type"), 50)
        if type_ not in RECIPIENT_TYPE_VALUES:
            raise RpcError("通知宛先の区分が不正です: " + type_)
        email = normalize_email(data.get("email"))
        if not email:
            raise RpcError("通知宛先のメールアドレスを入力してください。")
        active = data.get("active", None)
        normalized.append({
            "type": type_,
            "email": email,
            "name": sanitize_text(data.get("name"), 100),
            "active": True if active == "" or "active" not in data else parse_boolean(active),
            "send_as": "CC" if js_string(data.get("sendAs")).upper() == "CC" else "TO",
        })
    op.db.execute(delete(NotificationRecipient))
    for order, row in enumerate(normalized, start=1):
        op.db.add(NotificationRecipient(sort_order=order, **row))
    op.db.flush()


def save_settings(ctx: RpcContext, input: Any = None) -> dict[str, Any]:  # noqa: A002 - GAS 版と同じ名前
    op = Op(ctx)
    assert_admin(op)
    _save_settings(op, input)
    return build_bootstrap(op)


def save_approver_master(ctx: RpcContext, rows: Any = None) -> dict[str, Any]:
    op = Op(ctx)
    assert_admin(op)
    _save_approvers(op, rows)
    return build_bootstrap(op)


def save_notification_recipients(ctx: RpcContext, rows: Any = None) -> dict[str, Any]:
    op = Op(ctx)
    assert_admin(op)
    _save_recipients(op, rows)
    return build_bootstrap(op)


def save_admin_all(ctx: RpcContext, approvers: Any = None, recipients: Any = None, settings: Any = None) -> dict[str, Any]:
    """VPS 版で追加: 3 つの保存を 1 つのトランザクションで（途中で失敗したらどれも保存しない）。"""
    op = Op(ctx)
    assert_admin(op)
    _save_approvers(op, approvers)
    _save_recipients(op, recipients)
    _save_settings(op, settings)
    return build_bootstrap(op)


# ===== 外部シートからの手動同期 =====


def _sheets_api(db: Session, settings):
    """委任トークンで Sheets API を使う。テストではこの関数を差し替える。"""
    from google.auth.transport.requests import Request as GoogleRequest

    from app.services.sheets import SheetsApi

    creds = google_delegate.get_delegate_credentials(db, settings)

    def token() -> str:
        if not creds.valid:
            creds.refresh(GoogleRequest())
        return creds.token

    return SheetsApi(token)


def apply_sync_result(db: Session, kind: str, result, started_at: datetime) -> None:
    """同期の結果で表を置き換え（rows が None なら何もしない）、SyncRun に記録する。worker からも使える。"""
    if result.rows is not None:
        model = Worker if kind == SYNC_KIND_WORKERS else DeptSupervisor
        db.execute(delete(model))
        for row in result.rows:
            db.add(model(**row))
    message = result.error or ("元が空のため上書きしませんでした" if result.skipped else "")
    db.add(SyncRun(kind=kind, started_at=started_at, finished_at=now_utc(), ok=bool(result.ok),
                   row_count=int(result.count or 0), message=message))
    db.flush()


def _sync_now(ctx: RpcContext, kind: str) -> dict[str, Any]:
    from app.services import sync

    op = Op(ctx)
    assert_admin(op)
    started = now_utc()
    try:
        api = _sheets_api(op.db, ctx.settings)
    except google_delegate.DelegateNotAuthorized as error:
        result = sync.SyncResult(False, 0, fmt_datetime(started), error=str(error))
    else:
        result = sync.sync_workers(api) if kind == SYNC_KIND_WORKERS else sync.sync_dept_supervisors(api)
    apply_sync_result(op.db, kind, result, started)
    return result.to_client()


def sync_workers_now(ctx: RpcContext) -> dict[str, Any]:
    return _sync_now(ctx, SYNC_KIND_WORKERS)


def sync_dept_supervisors_now(ctx: RpcContext) -> dict[str, Any]:
    return _sync_now(ctx, SYNC_KIND_DEPT_SUPERVISORS)


# ===== 失敗した後処理 =====


def list_failed_jobs(ctx: RpcContext) -> list[dict[str, Any]]:
    op = Op(ctx)
    assert_admin(op)
    return outbox.list_failed(op.db)


def retry_failed_job(ctx: RpcContext, jobId: Any = None) -> dict[str, Any]:  # noqa: N803
    op = Op(ctx)
    assert_admin(op)
    return outbox.retry_failed(op.db, jobId)
