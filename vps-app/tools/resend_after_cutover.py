"""切り替えの直後に、進行中の申請へ依頼を送り直す（1 回だけ）。計画書 6-4・9-1・9-2（Q14）の決定。

- 承認中で 上席・総務部長・社長 の段: 今の承認者へ承認依頼を送り直す。
  * 上席: 上席マスタの全員（申請・再申請のときと同じ）
  * 総務部長: 代表 1 人（見積確定・至急のときと同じ）
  * 社長: 承認者マスタの社長の全員（総務部長の承認のときと同じ）
- 承認中で 購買見積・購買手配 の段: 新しいアプリで PDF を作り直し、添付して見積依頼・手配依頼を送り直す
  （前の PDF はドライブのゴミ箱へ。1 申請 = 1 ファイル）。
- 差戻し中・完了・取消には送らない。

仕組み: 承認・申請のときと同じ後回しの仕事（domain.outbox.defer → jobs.enqueue）を同じ kind・引数で積み、
worker が送る。積んだ時点の状態と段で照合するので、送る前に誰かが操作した申請には送らない。
宛先と件名は worker と同じ組み立て（JobRunner._build_mails）で出す。
二重に送らないよう、sync_runs に kind=resend_after_cutover の記録があれば止める（積むのと同じトランザクションで記録する）。

使い方（VPS。api のイメージにこのフォルダーを読み取り専用で付けて動かす。手順は docs/cutover-runbook.md）:
  python tools/resend_after_cutover.py --dry-run   # 送り先と件名の一覧だけ（DB に書かない）
  python tools/resend_after_cutover.py             # 積む
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "api", _ROOT):  # 手元（vps-app/api/app）とコンテナ（/app/app）の両方
    if (_p / "app").is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.constants import STATUS, STEPS  # noqa: E402
from app.domain import outbox  # noqa: E402
from app.domain.masters import Masters as DomainMasters  # noqa: E402
from app.jobs import DbRequestGateway, JobRunner, JobView  # noqa: E402
from app.models import Request, SyncRun  # noqa: E402
from app.serialize import request_row  # noqa: E402
from app.services.mail import effective_redirect  # noqa: E402
from app.services.mailpdf_common import MailPdfConfig, email_notifications_enabled  # noqa: E402
from app.timeutil import now_utc  # noqa: E402

SYNC_KIND = "resend_after_cutover"
ADVISORY_LOCK_KEY = 2026112501  # pg_advisory_xact_lock の鍵（この道具だけが使う）
ACTOR_NAME = "切り替え時の送り直し"
APPROVAL_STEPS = (STEPS.SUPERVISOR, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT)
PURCHASING_STEPS = (STEPS.PURCHASING_QUOTE, STEPS.PURCHASING)


class ResendStop(RuntimeError):
    """止める理由（二重の実行など）。"""


@dataclass
class Plan:
    request_id: str
    step: str
    kind: str = ""
    args: list = field(default_factory=list)
    mails: list = field(default_factory=list)  # OutgoingMail（宛先・件名の一覧用）
    note: str = ""


def already_ran(db: Session) -> SyncRun | None:
    return db.scalars(select(SyncRun).where(SyncRun.kind == SYNC_KIND).order_by(SyncRun.id)).first()


def _job_for(request: Request, masters: DomainMasters, actor: dict) -> tuple[str, list, str]:
    """(kind, args, note)。積まないときは kind が空。引数の並びは domain/workflow.py が積むものと同じ。"""
    snapshot = request_row(request)
    step = request.current_step
    current = {"email": request.current_approver_email or "", "name": request.current_approver_name or ""}
    if step == STEPS.GENERAL_MANAGER:
        # confirmQuote / confirmExpedited と同じく代表 1 人（承認者マスタの総務部長の先頭）
        rule = masters.find_approver_rule(request.applicant_email, request.department)
        try:
            gm = masters.get_approver_for_step(rule, STEPS.GENERAL_MANAGER) if rule else None
        except Exception:  # noqa: BLE001 - 承認者マスタに無ければ申請の今の承認者へ
            gm = None
        return "sendApprovalRequestEmail_", [snapshot, gm or current], ""
    if step in (STEPS.SUPERVISOR, STEPS.PRESIDENT) + PURCHASING_STEPS:
        # 承認でその段へ進んだときと同じ（上席・社長は段の全員へ承認依頼。購買は PDF を作って見積・手配の依頼）
        return "dispatchApprovalNotifications_", [snapshot, "", step, current, actor], ""
    return "", [], "送り直しの対象外の段です"


def plan_resends(session_factory: Callable[[], Session], config: MailPdfConfig, actor: dict) -> list[Plan]:
    gateway = DbRequestGateway(session_factory)
    services_masters = gateway.get_masters()
    preview = JobRunner(store=None, gateway=gateway, transport=None, storage=None, renderer=None,  # type: ignore[arg-type]
                        config=config)
    plans: list[Plan] = []
    with session_factory() as db:
        masters = DomainMasters.load(db)
        rows = db.scalars(select(Request).where(Request.status == STATUS.IN_REVIEW)
                          .order_by(Request.request_id)).all()
        for r in rows:
            kind, args, note = _job_for(r, masters, actor)
            plan = Plan(request_id=r.request_id, step=r.current_step, kind=kind, args=args, note=note)
            if kind:
                job = JobView(id=0, kind=kind, request_id=r.request_id, args=args, guard_status=r.status,
                              guard_step=r.current_step, attempts=0)
                plan.mails = preview._build_mails(job, services_masters)
                if not plan.mails:
                    plan.note = "宛先がありません（マスタか通知の設定を確かめてください）"
            plans.append(plan)
    return plans


def enqueue_plans(session_factory: Callable[[], Session], plans: list[Plan], now=None) -> int:
    """積む。sync_runs の記録と同じトランザクションにする（途中で失敗したら何も積まない）。"""
    now = now or now_utc()
    count = 0
    with session_factory() as db, db.begin():
        if db.get_bind().dialect.name == "postgresql":
            # 同時に 2 つ動かしても、記録の確認と積むのを 1 つずつにする（トランザクションの終わりで外れる）
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        if already_ran(db) is not None:
            raise ResendStop("送り直しは実行済みです（sync_runs に記録があります）。二重に送らないため止めました。")
        for plan in plans:
            if not plan.kind:
                continue
            request = db.scalars(select(Request).where(Request.request_id == plan.request_id).with_for_update()).first()
            if request is None or request.status != STATUS.IN_REVIEW or request.current_step != plan.step:
                continue  # 一覧を作った後に動いた申請は送らない
            outbox.defer(db, plan.kind, plan.args, guard=request)
            count += 1
        db.add(SyncRun(kind=SYNC_KIND, started_at=now, finished_at=now_utc(), ok=True, row_count=count,
                       message=", ".join(f"{p.request_id}:{p.step}" for p in plans if p.kind)[:2000]))
    return count


def format_plans(plans: list[Plan], config: MailPdfConfig, settings: dict) -> str:
    lines = []
    if not email_notifications_enabled(settings):
        lines.append("注意: 設定でメール通知が止まっています（enableEmailNotifications=false）。この状態では送られません。")
    try:
        redirect = effective_redirect(config, settings.get("mailRedirectTo"), now_utc())
    except Exception as error:  # noqa: BLE001
        lines.append(f"注意: 転送先を決められません: {error}")
        redirect = None
    if redirect is not None:
        lines.append(f"注意: すべて {redirect.email} へ転送されます（{config.app_env} の転送設定）。")
    total = 0
    for p in plans:
        if not p.kind:
            lines.append(f"- {p.request_id} [{p.step}] 送らない: {p.note}")
            continue
        if not p.mails:
            lines.append(f"- {p.request_id} [{p.step}] {p.kind}: {p.note}")
            continue
        for m in p.mails:
            total += 1
            pdf = "（PDF を作り直して添付）" if m.attach_pdf else ""
            cc = f" Cc: {m.cc_header()}" if m.cc else ""
            lines.append(f"- {p.request_id} [{p.step}] To: {m.to_header()}{cc} 件名: {m.subject}{pdf}")
    lines.append(f"申請 {sum(1 for p in plans if p.kind)} 件・メール {total} 通（承認中 {len(plans)} 件）")
    return "\n".join(lines)


def run(session_factory: Callable[[], Session], app_settings: Any, dry_run: bool) -> tuple[str, int]:
    config = MailPdfConfig.from_settings(app_settings)
    actor = {"email": app_settings.delegate_email, "name": ACTOR_NAME}
    with session_factory() as db:
        previous = already_ran(db)
    if previous is not None and not dry_run:
        raise ResendStop(f"送り直しは実行済みです（sync_runs id={previous.id}）。二重に送らないため止めました。")
    plans = plan_resends(session_factory, config, actor)
    text = format_plans(plans, config, DbRequestGateway(session_factory).get_settings())
    if previous is not None:
        text = f"注意: 送り直しは実行済みです（sync_runs id={previous.id}）。本番の実行は止まります。\n" + text
    if dry_run:
        return "dry-run（DB に書いていません）\n" + text, 0
    count = enqueue_plans(session_factory, plans)
    return text + f"\n積みました: {count} 件（worker が送ります。管理画面の「失敗した後処理」と mail_log で確かめてください）", count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="切り替えの直後に、進行中の申請へ依頼を送り直す（1 回だけ）")
    parser.add_argument("--dry-run", action="store_true", help="送り先と件名の一覧だけを出す（DB に書かない）")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.db import get_sessionmaker

    try:
        text, _ = run(get_sessionmaker(), get_settings(), args.dry_run)
    except ResendStop as error:
        print(f"止めました: {error}", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
