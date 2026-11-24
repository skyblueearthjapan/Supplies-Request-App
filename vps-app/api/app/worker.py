"""worker: 後回しの仕事（jobs）を回し、定期処理を呼ぶ。docker compose の worker サービスで `python -m app.worker`。

定期処理（中身は services/sync.py・services/sheets.py。ここは予定表と呼び出し口だけ）:
- 作業員マスタ・上席マスタの同期: 毎日 8 時台（日本時間）に 1 回（GAS 版のトリガーと同じ）
- スプレッドシートへの書き出し: 毎晩 2 時台に 1 回。戻せる期間（環境変数 EXPORT_HOURLY_UNTIL＝日本時間の
  日時まで。空なら毎晩だけ）は毎時
どれも表の丸ごとの置き換えなので、同じ枠で 2 回動いても（worker の再起動など）結果は同じ。
"""

from __future__ import annotations

import importlib
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.services.mailpdf_common import to_jst
from app.timeutil import now_utc, parse_jst_until

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0
SYNC_HOUR = 8
NIGHTLY_EXPORT_HOUR = 2

# 定期処理の関数（引数は session_factory。中で委任トークン・SheetsApi・DB の置き換え・SyncRun の記録まで行う）
SCHEDULED_HOOKS = {
    "sync_workers": ("app.services.sync", "run_scheduled_workers_sync"),
    "sync_dept_supervisors": ("app.services.sync", "run_scheduled_dept_supervisors_sync"),
    "export_sheets": ("app.services.sheets", "run_scheduled_export"),
}


def due_tasks(now: datetime, last_slots: dict[str, str], hourly_export_until: str = "") -> list[str]:
    """今の時刻で動かすべき定期処理の名前（同じ枠では 1 回だけ）。last_slots は更新する。

    hourly_export_until は EXPORT_HOURLY_UNTIL（日本時間の日時。日付だけならその日の終わりまで）。
    """
    jst = to_jst(now)
    until = parse_jst_until(hourly_export_until)
    due: list[str] = []
    if jst.hour == SYNC_HOUR:
        slot = jst.strftime("%Y-%m-%d")
        for name in ("sync_workers", "sync_dept_supervisors"):
            if last_slots.get(name) != slot:
                last_slots[name] = slot
                due.append(name)
    hourly = until is not None and jst < until
    if hourly or jst.hour == NIGHTLY_EXPORT_HOUR:
        slot = jst.strftime("%Y-%m-%d %H") if hourly else jst.strftime("%Y-%m-%d")
        if last_slots.get("export_sheets") != slot:
            last_slots["export_sheets"] = slot
            due.append("export_sheets")
    return due


def default_hooks(session_factory: Any) -> dict[str, Callable[[], object]]:
    def call(module: str, fn: str) -> Callable[[], object]:
        def run() -> object:
            target = getattr(importlib.import_module(module), fn, None)
            if target is None:
                log.warning("scheduled hook not implemented yet: %s.%s", module, fn)
                return None
            return target(session_factory)

        return run

    return {name: call(mod, fn) for name, (mod, fn) in SCHEDULED_HOOKS.items()}


@dataclass
class Worker:
    run_jobs_once: Callable[[], dict]  # JobRunner.run_once
    hooks: dict[str, Callable[[], object]] = field(default_factory=dict)
    hourly_export_until: str = ""
    now: Callable[[], datetime] = now_utc
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


def build_runner(session_factory: Any = None, app_settings: Any = None, renderer: Any = None):
    """本番の部品で JobRunner を組み立てる（worker と、W の generateRequestPdf・取消のゴミ箱でも使う）。"""
    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.google_delegate import get_delegate_credentials
    from app.jobs import DbRequestGateway, JobRunner, JobStore
    from app.services.drive import DriveClient, PdfStorage
    from app.services.mail import GmailApiTransport
    from app.services.mailpdf_common import MailPdfConfig
    from app.services.pdf import PdfRenderer

    s = app_settings or get_settings()
    sf = session_factory or get_sessionmaker()

    def credentials():
        with sf() as db:
            return get_delegate_credentials(db, s)

    return JobRunner(
        store=JobStore(sf),
        gateway=DbRequestGateway(sf),
        transport=GmailApiTransport(credentials),
        storage=PdfStorage(DriveClient(credentials), s),
        renderer=renderer or PdfRenderer(),
        config=MailPdfConfig.from_settings(s),
        extra_admins=s.bootstrap_admin_list,
    )


def main() -> None:  # pragma: no cover - VPS で動かす
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.db import get_sessionmaker

    sf = get_sessionmaker()
    runner = build_runner(sf)
    from app.config import get_settings

    worker = Worker(run_jobs_once=runner.run_once, hooks=default_hooks(sf),
                    hourly_export_until=get_settings().export_hourly_until)
    try:
        worker.run_forever()
    finally:
        runner.renderer.close()


if __name__ == "__main__":  # pragma: no cover
    main()
