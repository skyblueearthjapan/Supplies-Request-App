"""worker の定期処理の枠と、ドライブの置き場・staging の安全装置。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import drive as drive_mod
from app.services.drive import DriveClient, DriveError, PdfStorage, date_folder_name, pdf_root_folder_id
from app.services.mailpdf_common import MailPdfConfig
from app.worker import Worker, due_tasks

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


# ---------------------------------------------------------------------------
# ドライブ
# ---------------------------------------------------------------------------

STAGING = MailPdfConfig(app_env="staging", staging_pdf_folder_id="STG_ROOT")
PROD = MailPdfConfig(app_env="production")


def test_root_folder_staging_ignores_settings():
    assert pdf_root_folder_id(STAGING, {"pdfFolderId": "PROD_ROOT"}) == "STG_ROOT"
    assert pdf_root_folder_id(PROD, {"pdfFolderId": " PROD_ROOT "}) == "PROD_ROOT"
    with pytest.raises(DriveError):
        pdf_root_folder_id(PROD, {"pdfFolderId": ""})  # GAS 版のように勝手に作らない
    with pytest.raises(DriveError):
        pdf_root_folder_id(MailPdfConfig(app_env="staging"), {"pdfFolderId": "PROD_ROOT"})


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


@pytest.fixture
def staging_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("STAGING_PDF_FOLDER_ID", "STG_ROOT")
    monkeypatch.setenv("PRODUCTION_PDF_FOLDER_ID", "PROD_ROOT")


def make_storage(parents, config=STAGING):
    sess = FakeDriveSession(parents)
    return PdfStorage(DriveClient(lambda: "creds", session_factory=lambda c: sess), config), sess


def test_staging_saves_under_staging_folder(staging_env):
    storage, sess = make_storage({"STG_ROOT": []})
    f = storage.save({"pdfFolderId": "PROD_ROOT"}, "2026-11-24", "a.pdf", b"%PDF")
    assert f.id == "NEWPDF"
    created = [c for c in sess.calls if c[0] == "POST" and "upload" not in c[1]][0]
    assert created[2] == {"name": "2026-11-24", "mimeType": "application/vnd.google-apps.folder", "parents": ["STG_ROOT"]}


def test_staging_never_writes_to_production_folder(staging_env):
    storage, _ = make_storage({})
    storage.config = MailPdfConfig(app_env="staging", staging_pdf_folder_id="PROD_ROOT")  # 設定の誤り
    with pytest.raises(RuntimeError):
        storage.save({}, "2026-11-24", "a.pdf", b"%PDF")


def test_staging_trash_only_inside_staging_folder(staging_env):
    storage, sess = make_storage({"OLD_PROD_PDF": ["PROD_DATE"], "PROD_DATE": ["PROD_ROOT"],
                                  "STG_PDF": ["STG_DATE"], "STG_DATE": ["STG_ROOT"]})
    assert storage.trash({}, "OLD_PROD_PDF") is False  # 移したデータが指す本番の PDF は消さない
    assert storage.trash({}, "STG_PDF") is True
    assert [c[1].rsplit("/", 1)[1] for c in sess.calls if c[0] == "PATCH"] == ["STG_PDF"]
    assert storage.trash({}, "") is False


def test_production_trash_and_missing_file_is_ignored(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    storage, sess = make_storage({"X": ["D"]}, PROD)
    assert storage.trash({"pdfFolderId": "PROD_ROOT"}, "X") is True
    sess.patch = lambda *a, **k: Resp(404)
    assert storage.trash({"pdfFolderId": "PROD_ROOT"}, "GONE") is False


def test_fallback_safety_function_exists():
    assert callable(drive_mod.assert_writable_drive_folder)
