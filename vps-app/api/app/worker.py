"""worker: 後回しの仕事（jobs）を回し、定期処理を呼ぶ。docker compose の worker サービスで `python -m app.worker`。

定期処理（中身は担当 D の services/sync.py・services/sheets.py。ここは呼び出し口だけ）:
- 作業員マスタ・上席マスタの同期: 毎日 8 時台（日本時間）に 1 回（GAS 版のトリガーと同じ）
- スプレッドシートへの書き出し: 毎晩 2 時台に 1 回。戻せる期間（SHEETS_EXPORT_HOURLY_UNTIL=yyyy-MM-dd まで）は毎時
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .services.mailpdf_common import to_jst

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
SYNC_HOUR = 8
NIGHTLY_EXPORT_HOUR = 2


def _hourly_export_active(now_jst: datetime, until: str) -> bool:
    return bool(until) and now_jst.strftime("%Y-%m-%d") <= until


def due_tasks(now: datetime, last_slots: dict[str, str], hourly_export_until: str = "") -> list[str]:
    """今の時刻で動かすべき定期処理の名前（同じ枠では 1 回だけ）。last_slots は更新する。"""
    jst = to_jst(now)
    due: list[str] = []
    if jst.hour == SYNC_HOUR:
        slot = jst.strftime("%Y-%m-%d")
        for name in ("sync_workers", "sync_dept_supervisors"):
            if last_slots.get(name) != slot:
                last_slots[name] = slot
                due.append(name)
    if _hourly_export_active(jst, hourly_export_until):
        slot = jst.strftime("%Y-%m-%d %H")
        if last_slots.get("export_sheets") != slot:
            last_slots["export_sheets"] = slot
            due.append("export_sheets")
    elif jst.hour == NIGHTLY_EXPORT_HOUR:
        slot = jst.strftime("%Y-%m-%d")
        if last_slots.get("export_sheets") != slot:
            last_slots["export_sheets"] = slot
            due.append("export_sheets")
    return due


def default_hooks() -> dict[str, Callable[[], object]]:
    """担当 D の関数を呼ぶ口。名前が決まったらここを合わせる（無ければ記録だけして飛ばす）。"""

    def call(module: str, fn: str) -> Callable[[], object]:
        def run() -> object:
            import importlib

            mod = importlib.import_module(module)
            target = getattr(mod, fn, None)
            if target is None:
                log.warning("scheduled hook not implemented yet: %s.%s", module, fn)
                return None
            return target()

        return run

    return {
        "sync_workers": call("app.services.sync", "sync_workers_master"),
        "sync_dept_supervisors": call("app.services.sync", "sync_dept_supervisors_master"),
        "export_sheets": call("app.services.sheets", "export_all"),
    }


@dataclass
class Worker:
    run_jobs_once: Callable[[], dict]  # JobRunner.run_once
    hooks: dict[str, Callable[[], object]] = field(default_factory=default_hooks)
    hourly_export_until: str = ""
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    last_slots: dict[str, str] = field(default_factory=dict)
    stopping: bool = False

    def tick(self) -> None:
        for name in due_tasks(self.now(), self.last_slots, self.hourly_export_until):
            try:
                log.info("scheduled task start: %s", name)
                self.hooks[name]()
            except Exception:
                log.exception("scheduled task failed: %s", name)
        try:
            self.run_jobs_once()
        except Exception:
            log.exception("job loop failed")

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopping", True))
        while not self.stopping:
            self.tick()
            time.sleep(POLL_SECONDS)


def main() -> None:  # pragma: no cover - VPS で動かす
    """組み立て。F の db・google_delegate、W の gateway ができたらここをつなぐ。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .jobs import JobRunner, SqlJobStore
    from .services.drive import DriveClient, PdfStorage
    from .services.mail import GmailApiTransport
    from .services.mailpdf_common import MailPdfConfig
    from .services.pdf import PdfRenderer

    from . import db, models  # type: ignore  # 担当 F
    from .google_delegate import get_delegate_credentials  # type: ignore  # 担当 F
    from .domain.job_gateway import DbRequestGateway  # type: ignore  # 担当 W（RequestGateway の実装）

    config = MailPdfConfig.from_env()
    store = SqlJobStore(db.SessionLocal, models.Job, models.MailLog)
    renderer = PdfRenderer()
    runner = JobRunner(
        store=store,
        gateway=DbRequestGateway(db.SessionLocal),
        transport=GmailApiTransport(get_delegate_credentials),
        storage=PdfStorage(DriveClient(get_delegate_credentials), config),
        renderer=renderer,
        config=config,
        extra_admins=[e.strip() for e in (os.environ.get("BOOTSTRAP_ADMINS") or "").split(",") if e.strip()],
    )
    worker = Worker(run_jobs_once=runner.run_once, hourly_export_until=os.environ.get("SHEETS_EXPORT_HOURLY_UNTIL", ""))
    try:
        worker.run_forever()
    finally:
        renderer.close()


if __name__ == "__main__":  # pragma: no cover
    main()
