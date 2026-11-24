"""後回しの仕事を積む（Jobs.gs の defer_ の代わり）。実行は担当 M の worker。

- 状態の変更と同じトランザクションで models.Job を insert する（commit は rpc.py）。
- kind は GAS の関数名（DEFERRABLE_JOBS と同じ 10 種）と、VPS 版で足した regeneratePdf・trashPdf。
- args_json は GAS 版の defer_ と同じ並びの引数（申請は serialize.request_row の camelCase の dict）。
- guard_status / guard_step は積んだ時点（状態を書き換えた後）の申請の値。照合しない仕事は空。

pending_count / list_failed / retry_failed は、担当 M の jobs.py が同じ名前で用意する予定のもの。
まだ無いので、ここで models を直接使って作っている（まとめるときに M の方へ寄せてよい）。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, Request
from app.rpc import RpcError
from app.serialize import fmt_datetime
from app.timeutil import now_utc

# GAS 版 Jobs.gs の DEFERRABLE_JOBS と同じ 10 種 ＋ VPS 版で足したもの
DEFERRABLE_KINDS = (
    "sendApprovalRequestEmail_",
    "sendRequestUpdatedEmail_",
    "sendRejectedEmail_",
    "sendReturnedEmail_",
    "sendReturnedBroadcast_",
    "notifyGeneralAffairs_",
    "notifySplitReturned_",
    "dispatchApprovalNotifications_",
    "completeAndNotify_",
    "regeneratePdfAfterSplit_",
    "regeneratePdf",  # VPS 版: 手動の PDF 再作成（generateRequestPdf）
    "trashPdf",       # VPS 版: 取消・却下した申請の PDF をドライブのゴミ箱へ（GAS 版はその場で消していた）
)

KIND_LABELS = {
    "sendApprovalRequestEmail_": "承認依頼メール",
    "sendRequestUpdatedEmail_": "明細修正の通知メール",
    "sendRejectedEmail_": "却下の通知メール",
    "sendReturnedEmail_": "差戻しの通知メール",
    "sendReturnedBroadcast_": "差戻しの一斉共有メール",
    "notifyGeneralAffairs_": "総務部への通知メール",
    "notifySplitReturned_": "分割した明細の差戻し通知",
    "dispatchApprovalNotifications_": "承認後の通知・PDF作成",
    "completeAndNotify_": "完了の通知・PDF作成",
    "regeneratePdfAfterSplit_": "分割後のPDF作り直し",
    "regeneratePdf": "PDF再作成",
    "trashPdf": "PDFをゴミ箱へ移す",
}

OPEN_STATES = ("pending", "running")


def defer(
    db: Session,
    kind: str,
    args: list[Any],
    *,
    guard: Request | None = None,
    request_id: str = "",
) -> Job:
    """defer_(fnName, args, guardRequestId)。guard を渡すと、その時点の状態と段を記録する。"""
    if kind not in DEFERRABLE_KINDS:
        raise ValueError("後回しにできない関数です: " + kind)
    at = now_utc()
    job = Job(
        kind=kind,
        args_json=json.dumps(args, ensure_ascii=False, separators=(",", ":")),
        request_id=(guard.request_id if guard is not None else request_id) or "",
        guard_status=guard.status if guard is not None else "",
        guard_step=guard.current_step if guard is not None else "",
        state="pending",
        attempts=0,
        last_error="",
        run_after=at,
        created_at=at,
        updated_at=at,
    )
    db.add(job)
    db.flush()
    return job


def pending_count(db: Session, request_id: str) -> int:
    """その申請のまだ終わっていない仕事の数（詳細の pendingJobs）。"""
    return int(
        db.execute(
            select(func.count()).select_from(Job).where(Job.request_id == request_id, Job.state.in_(OPEN_STATES))
        ).scalar_one()
    )


def list_failed(db: Session) -> list[dict[str, Any]]:
    """管理画面の「失敗した後処理」。新しい順。"""
    rows = db.execute(select(Job).where(Job.state == "failed").order_by(Job.updated_at.desc(), Job.id.desc())).scalars()
    return [
        {
            "jobId": str(job.id),
            "kind": job.kind,
            "label": KIND_LABELS.get(job.kind, job.kind),
            "requestId": job.request_id,
            "attempts": job.attempts,
            "error": (job.last_error or "")[:500],
            "failedAt": fmt_datetime(job.finished_at or job.updated_at),
        }
        for job in rows
    ]


def retry_failed(db: Session, job_id: Any) -> dict[str, Any]:
    """失敗した仕事をもう一度積む（state を pending に戻す）。メールは未送信の分だけ送る（M の mail_log で判定）。"""
    try:
        jid = int(str(job_id).strip())
    except (TypeError, ValueError):
        raise RpcError("やり直せる後処理が見つかりません。") from None
    job = db.execute(select(Job).where(Job.id == jid).with_for_update()).scalar_one_or_none()
    if job is None or job.state != "failed":
        raise RpcError("やり直せる後処理が見つかりません。")
    at = now_utc()
    job.state = "pending"
    job.attempts = 0  # 自動の再試行の回数を数え直す（last_error は残す）
    job.run_after = at
    job.updated_at = at
    job.finished_at = None
    db.flush()
    return {"jobId": str(job.id), "state": job.state}
