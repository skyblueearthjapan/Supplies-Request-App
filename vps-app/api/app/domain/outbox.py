"""後回しの仕事を積む（Jobs.gs の defer_ の代わり）。実行は担当 M の worker。

- 状態の変更と同じトランザクションで models.Job を insert する（commit は rpc.py）。
- kind は GAS の関数名（DEFERRABLE_JOBS と同じ 10 種）と、VPS 版で足した regeneratePdf・trashPdf。
- args_json は GAS 版の defer_ と同じ並びの引数（申請は serialize.request_row の camelCase の dict）。
- guard_status / guard_step は積んだ時点（状態を書き換えた後）の申請の値。照合しない仕事は空。

積むのは models.Job を直接 insert する（担当 M の jobs.enqueue は regeneratePdf・trashPdf をまだ受け付けないため）。
pending_count / list_failed / retry_failed は担当 M の app.jobs を使う。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.models import Job, Request
from app.rpc import RpcError
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
    """その申請のまだ終わっていない仕事の数（詳細の pendingJobs）。担当 M の jobs.pending_count。"""
    return int(jobs.pending_count(db, request_id))


def list_failed(db: Session) -> list[dict[str, Any]]:
    """管理画面の「失敗した後処理」。担当 M の jobs.list_failed（jobId は画面で文字として扱うので文字にする）。"""
    rows = jobs.list_failed(db)
    for row in rows:
        row["jobId"] = str(row["jobId"])
        if not row.get("label") or row.get("label") == row.get("kind"):
            row["label"] = KIND_LABELS.get(row.get("kind", ""), row.get("kind", ""))
    return rows


def retry_failed(db: Session, job_id: Any) -> dict[str, Any]:
    """失敗した仕事をもう一度積む。担当 M の jobs.retry_failed（未送信のメールだけ送る）。"""
    try:
        jid = int(str(job_id).strip())
    except (TypeError, ValueError):
        raise RpcError("やり直せる後処理が見つかりません。") from None
    db.execute(select(Job).where(Job.id == jid).with_for_update()).scalar_one_or_none()
    if not jobs.retry_failed(db, jid):
        raise RpcError("やり直せる後処理が見つかりません。")
    db.flush()
    return {"jobId": str(jid), "state": "pending"}
