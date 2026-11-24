"""worker の定期処理の枠と、ドライブの置き場・staging の安全装置（app.safety）。

conftest の環境: STAGING_PDF_FOLDER_ID=staging-folder-id、PROD_PDF_FOLDER_ID=prod-folder-id。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_settings

from app.safety import UnsafeWriteError
from app.services.drive import DriveClient, PdfStorage, date_folder_name
from app.worker import SCHEDULED_HOOKS, Worker, default_hooks, due_tasks

JST = timezone(timedelta(hours=9))


def at(h, m=0, day=24):
    return datetime(2026, 11, day, h, m, tzinfo=JST)


def test_sync_runs_once_in_8_oclock_hour_jst():
    slots: dict[str, str] = {}
    assert due_tasks(at(7, 59), slots) == []
    assert due_tasks(at(8, 0), slots) == ["sync_workers", "sync_dept_supervisors"]
    assert due_tasks(at(8, 30), slots) == []
    assert due_tasks(at(8, 5, day=25), slots) == ["sync_workers", "sync_dept_supervisors"]
    # UTC で渡しても日本時間で判定する（23:10 UTC = 8:10 JST）
    assert due_tasks(datetime(2026, 11, 26, 23, 10, tzinfo=timezone.utc), {}) == ["sync_workers", "sync_dept_supervisors"]


def test_sheet_export_nightly_or_hourly_during_rollback_window():
    slots: dict[str, str] = {}
    assert due_tasks(at(1, 59), slots) == []
    assert due_tasks(at(2, 0), slots) == ["export_sheets"]
    assert due_tasks(at(2, 40), slots) == []
    hourly: dict[str, str] = {}
    assert due_tasks(at(13, 1), hourly, "2026-11-28") == ["export_sheets"]
    assert due_tasks(at(13, 59), hourly, "2026-11-28") == []
    assert due_tasks(at(14, 0), hourly, "2026-11-28") == ["export_sheets"]
    assert due_tasks(at(14, 0, day=29), hourly, "2026-11-28") == []  # 期間が過ぎたら毎晩だけ


def test_export_hourly_until_is_jst_datetime_then_nightly():
    until = "2026-11-28 12:00"  # EXPORT_HOURLY_UNTIL（日本時間）
    slots: dict[str, str] = {}
    assert due_tasks(at(11, 59, day=28), slots, until) == ["export_sheets"]
    assert due_tasks(at(12, 0, day=28), slots, until) == []  # その時刻からは毎晩だけ
    assert due_tasks(at(23, 0, day=28), slots, until) == []
    assert due_tasks(at(2, 0, day=29), slots, until) == ["export_sheets"]
    assert due_tasks(at(2, 30, day=29), slots, until) == []
    assert due_tasks(at(3, 0, day=29), slots, until) == []
    # UTC で渡しても日本時間で判定する（02:59 UTC = 11:59 JST）
    assert due_tasks(datetime(2026, 11, 28, 2, 59, tzinfo=timezone.utc), {}, until) == ["export_sheets"]
    # 空なら毎晩だけ
    assert due_tasks(at(11, 0), {}, "") == []


def test_export_hourly_until_setting_is_checked_at_startup():
    from pydantic import ValidationError

    assert make_settings().export_hourly_until == ""
    assert make_settings(export_hourly_until=" 2026-12-11 18:00 ").export_hourly_until == "2026-12-11 18:00"
    with pytest.raises(ValidationError):
        make_settings(export_hourly_until="来週の金曜")


def test_worker_runs_export_every_hour_until_setting():
    calls = []
    clock = {"t": at(9, 5, day=28)}
    w = Worker(run_jobs_once=lambda: {}, hooks={"export_sheets": lambda: calls.append(clock["t"].hour)},
               hourly_export_until="2026-11-28 11:00", now=lambda: clock["t"])
    for h, m in [(9, 5), (9, 50), (10, 0), (10, 30), (11, 0), (12, 0)]:
        clock["t"] = at(h, m, day=28)
        w.tick()
    clock["t"] = at(2, 1, day=29)
    w.tick()
    w.tick()
    assert calls == [9, 10, 2]


def test_worker_tick_calls_hooks_and_jobs_and_survives_errors():
    calls = []

    def boom():
        calls.append("sync_workers")
        raise RuntimeError("x")

    w = Worker(run_jobs_once=lambda: calls.append("jobs") or {},
               hooks={"sync_workers": boom, "sync_dept_supervisors": lambda: calls.append("dept"),
                      "export_sheets": lambda: calls.append("export")},
               now=lambda: at(8, 1))
    w.tick()
    w.tick()
    assert calls == ["sync_workers", "dept", "jobs", "jobs"]


def test_default_hooks_skip_missing_functions():
    hooks = default_hooks(session_factory=None)
    assert set(hooks) == set(SCHEDULED_HOOKS)
    for name, (module, fn) in SCHEDULED_HOOKS.items():
        import importlib

        if getattr(importlib.import_module(module), fn, None) is None:
            assert hooks[name]() is None  # まだ無ければ記録して飛ばす（worker は止めない）


# ---------------------------------------------------------------------------
# ドライブ
# ---------------------------------------------------------------------------

def test_date_folder_name():
    assert date_folder_name("2026-11-24") == "2026-11-24"
    assert date_folder_name("2026-11-24 09:00:00") == "2026-11-24"
    assert len(date_folder_name("")) == 10


class Resp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code = status
        self._data = data or {}
        self.text = ""
        self.content = content

    def json(self):
        return self._data


class FakeDriveSession:
    """parents の表と呼び出しの記録だけを持つ Drive API の偽物。"""

    def __init__(self, parents):
        self.parents = parents
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        if url.endswith("/files"):
            return Resp(data={"files": []})
        fid = url.rsplit("/", 1)[1]
        if fid not in self.parents:
            return Resp(404)
        return Resp(data={"parents": self.parents[fid]})

    def post(self, url, params=None, json=None, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json or headers))
        if "upload" in url:
            return Resp(data={"id": "NEWPDF", "webViewLink": "https://drive.google.com/file/d/NEWPDF/view"})
        self.parents["DATEFOLDER"] = [json["parents"][0]]
        return Resp(data={"id": "DATEFOLDER"})

    def patch(self, url, params=None, json=None, timeout=None):
        self.calls.append(("PATCH", url, json))
        return Resp()


def make_storage(parents, settings=None):
    sess = FakeDriveSession(parents)
    return PdfStorage(DriveClient(lambda: "creds", session_factory=lambda c: sess), settings or make_settings()), sess


def test_staging_saves_under_staging_folder_not_settings_folder():
    storage, sess = make_storage({"staging-folder-id": []})
    f = storage.save({"pdfFolderId": "prod-folder-id"}, "2026-11-24", "a.pdf", b"%PDF")
    assert f.id == "NEWPDF"
    created = [c for c in sess.calls if c[0] == "POST" and "upload" not in c[1]][0]
    assert created[2] == {"name": "2026-11-24", "mimeType": "application/vnd.google-apps.folder",
                          "parents": ["staging-folder-id"]}


def test_staging_never_writes_to_production_folder():
    s = make_settings()
    s.staging_pdf_folder_id = "prod-folder-id"  # 起動時の検査をすり抜けた設定の誤り（config は起動で止めるが二重に守る）
    storage, sess = make_storage({}, s)
    with pytest.raises(UnsafeWriteError):
        storage.save({}, "2026-11-24", "a.pdf", b"%PDF")
    assert not [c for c in sess.calls if c[0] == "POST"]


def test_production_saves_under_settings_folder_and_refuses_empty():
    storage, sess = make_storage({}, make_settings(app_env="production", mail_redirect_to=""))
    storage.save({"pdfFolderId": "prod-folder-id"}, "2026-11-24", "a.pdf", b"%PDF")
    created = [c for c in sess.calls if c[0] == "POST" and "upload" not in c[1]][0]
    assert created[2]["parents"] == ["prod-folder-id"]
    with pytest.raises(UnsafeWriteError):
        storage.save({"pdfFolderId": ""}, "2026-11-24", "a.pdf", b"%PDF")  # GAS 版のように勝手に作らない


def test_staging_trash_only_inside_staging_folder():
    storage, sess = make_storage({"OLD_PROD_PDF": ["PROD_DATE"], "PROD_DATE": ["prod-folder-id"],
                                  "STG_PDF": ["STG_DATE"], "STG_DATE": ["staging-folder-id"]})
    assert storage.trash({}, "OLD_PROD_PDF") is False  # 移したデータが指す本番の PDF は消さない
    assert storage.trash({}, "STG_PDF") is True
    assert storage.trash({}, "GONE") is False
    assert storage.trash({}, "") is False
    assert [c[1].rsplit("/", 1)[1] for c in sess.calls if c[0] == "PATCH"] == ["STG_PDF"]


def test_production_trash():
    storage, sess = make_storage({"X": ["D"], "D": ["prod-folder-id"]}, make_settings(app_env="production", mail_redirect_to=""))
    assert storage.trash({"pdfFolderId": "prod-folder-id"}, "X") is True
    sess.patch = lambda *a, **k: Resp(404)
    assert storage.trash({}, "X") is False
