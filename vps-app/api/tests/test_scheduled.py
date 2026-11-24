"""worker の定期処理: マスタの同期（毎朝 8 時台）とスプレッドシートへの書き出し（毎晩。戻せる期間は毎時）。

委任トークンの Sheets API は偽物（test_sheets_sync.FakeSheetsApi）に差し替える。DB は SQLite。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from test_jobs import seed_masters, seed_request
from test_sheets_sync import FakeSheetsApi

from app import models, safety
from app.domain import admin
from app.services import sheet_codec as codec
from app.services import sheets, sync


def runs(session_factory, kind):
    with session_factory() as db:
        return db.scalars(select(models.SyncRun).where(models.SyncRun.kind == kind).order_by(models.SyncRun.id)).all()


def workers(session_factory):
    with session_factory() as db:
        rows = db.scalars(select(models.Worker).order_by(models.Worker.sort_order)).all()
        return [(w.worker_code, w.name, w.dept, w.location, w.staff_type) for w in rows]


def supervisors(session_factory):
    with session_factory() as db:
        rows = db.scalars(select(models.DeptSupervisor).order_by(models.DeptSupervisor.sort_order)).all()
        return [(r.department, r.email, r.name) for r in rows]


@pytest.fixture
def old_masters(session_factory):
    with session_factory() as db:
        db.add(models.Worker(sort_order=1, worker_code="OLD", name="古い人", dept="", location="", staff_type=""))
        db.add(models.DeptSupervisor(sort_order=1, department="旧部", email="old@x", name="旧", active=True,
                                     title="", notify_email=""))
        db.commit()


def use_api(monkeypatch, api):
    monkeypatch.setattr(admin, "_sheets_api", lambda db, settings: api)


# ===== 同期 =====


def test_sync_without_delegate_records_failure_and_keeps_tables(session_factory, old_masters):
    # 委任がまだ無い（google_credentials が空）: 例外にせず、失敗として記録して飛ばす
    result = sync.run_scheduled_workers_sync(session_factory)
    assert not result.ok and result.rows is None
    sync.run_scheduled_dept_supervisors_sync(session_factory)
    (w,) = runs(session_factory, "workers")
    (d,) = runs(session_factory, "dept_supervisors")
    assert (w.ok, w.row_count) == (False, 0) and "委任" in w.message
    assert (d.ok, d.row_count) == (False, 0) and "委任" in d.message
    assert workers(session_factory) == [("OLD", "古い人", "", "", "")]
    assert supervisors(session_factory) == [("旧部", "old@x", "旧")]


def test_sync_with_delegate_replaces_tables_and_is_idempotent(session_factory, old_masters, monkeypatch):
    use_api(monkeypatch, FakeSheetsApi({
        "作業員": [["コード"], ["0001", "山田", "機械部", "", "本社", "社員"], ["0002", "佐藤", "電気部", "", "工場", "派遣"]],
    }, gids={"作業員": sync.MASTER_WORKER_GID}))
    for _ in range(2):  # 2 回動いても同じ結果
        result = sync.run_scheduled_workers_sync(session_factory)
        assert result.ok and result.count == 2
        assert workers(session_factory) == [("0001", "山田", "機械部", "本社", "社員"), ("0002", "佐藤", "電気部", "工場", "派遣")]
    assert [(r.ok, r.row_count, r.message) for r in runs(session_factory, "workers")] == [(True, 2, ""), (True, 2, "")]

    use_api(monkeypatch, FakeSheetsApi({"DeptApprovers": [["部署", "メール", "氏名"], ["機械部", "A@x", "上田"]]}))
    sync.run_scheduled_dept_supervisors_sync(session_factory)
    sync.run_scheduled_dept_supervisors_sync(session_factory)
    assert supervisors(session_factory) == [("機械部", "a@x", "上田")]
    assert [(r.ok, r.row_count) for r in runs(session_factory, "dept_supervisors")] == [(True, 1), (True, 1)]


def test_sync_rows_none_keeps_tables(session_factory, old_masters, monkeypatch):
    use_api(monkeypatch, FakeSheetsApi({"作業員": [["コード"]], "DeptApprovers": [["部署", "メール"]]},
                                       gids={"作業員": sync.MASTER_WORKER_GID}))
    assert sync.run_scheduled_workers_sync(session_factory).rows is None
    assert sync.run_scheduled_dept_supervisors_sync(session_factory).rows is None
    assert workers(session_factory) == [("OLD", "古い人", "", "", "")]
    assert supervisors(session_factory) == [("旧部", "old@x", "旧")]
    (w,) = runs(session_factory, "workers")
    assert (w.ok, w.row_count, w.message) == (True, 0, "元が空のため上書きしませんでした")

    # 読めない（タブが無い）ときも上書きしない
    use_api(monkeypatch, FakeSheetsApi({"別": [["a"]]}))
    sync.run_scheduled_workers_sync(session_factory)
    assert workers(session_factory) == [("OLD", "古い人", "", "", "")]
    assert runs(session_factory, "workers")[-1].ok is False


# ===== 書き出し =====


class RecordingSheetsApi(FakeSheetsApi):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ids = set()

    def get_spreadsheet(self, spreadsheet_id):
        self.ids.add(spreadsheet_id)
        return super().get_spreadsheet(spreadsheet_id)

    def batch_update_values(self, spreadsheet_id, data):
        self.ids.add(spreadsheet_id)
        return super().batch_update_values(spreadsheet_id, data)

    def batch_clear(self, spreadsheet_id, ranges):
        self.ids.add(spreadsheet_id)
        return super().batch_clear(spreadsheet_id, ranges)


def export_api():
    tabs = {codec.TABLES[t].sheet: [codec.TABLES[t].headers] for t in codec.EXPORT_ORDER}
    tabs[codec.TABLES["requests"].sheet] += [["OLD-1"], ["OLD-2"], ["OLD-3"]]  # 前回の余りの行
    return RecordingSheetsApi(tabs)


@pytest.fixture
def seeded(session_factory):
    with session_factory() as db:
        seed_masters(db)
        db.add(models.Worker(sort_order=1, worker_code="0001", name="山田", dept="機械部", location="本社", staff_type="社員"))
        db.commit()
        return seed_request(db)


def test_export_without_delegate_records_failure(session_factory, seeded):
    assert sheets.run_scheduled_export(session_factory) is None
    (r,) = runs(session_factory, "export")
    assert (r.ok, r.row_count) == (False, 0) and "委任" in r.message


def test_export_writes_all_tables_to_staging_copy_and_is_idempotent(session_factory, seeded, monkeypatch):
    api = export_api()
    use_api(monkeypatch, api)
    first = sheets.run_scheduled_export(session_factory)
    snapshot = {k: [list(r) for r in v] for k, v in api.tabs.items()}
    second = sheets.run_scheduled_export(session_factory)
    assert api.tabs == snapshot  # 2 回動いても同じ
    assert api.ids == {"staging-sheet-id"}  # staging はステージング用のコピーへ（本番の ID には書かない）
    assert first.counts == second.counts
    assert first.counts["requests"] == 1 and first.counts["request_items"] == 2 and first.counts["history"] == 1
    assert first.counts["workers"] == 1 and first.counts["settings"] == 4
    req_tab = api.tabs[codec.TABLES["requests"].sheet]
    assert len(req_tab) == 2 and req_tab[1][0] == seeded["requestId"]  # 余りの OLD 行は消えた
    assert api.tabs[codec.TABLES["workers"].sheet][1] == ["0001", "山田", "機械部", "本社", "社員"]
    rs = runs(session_factory, "export")
    assert [r.ok for r in rs] == [True, True]
    assert rs[0].row_count == sum(first.counts.values()) and rs[0].message.startswith("staging-sheet-id: ")


def test_export_refused_by_safety_is_recorded_and_raised(session_factory, seeded, monkeypatch):
    api = export_api()
    use_api(monkeypatch, api)

    def refuse(settings=None):
        raise safety.UnsafeWriteError("ステージングから本番のスプレッドシートへは書き込めません")

    monkeypatch.setattr(safety, "export_spreadsheet_id", refuse)
    with pytest.raises(safety.UnsafeWriteError):
        sheets.run_scheduled_export(session_factory)
    assert api.updates == [] and api.clears == []
    (r,) = runs(session_factory, "export")
    assert r.ok is False and "UnsafeWriteError" in r.message
