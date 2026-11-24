"""本番スプレッドシート → DB の移行（スプレッドシートは読むだけ）。

使い方（例）:
  # 1) 読んで確かめるだけ（DB に書かない）
  python tools/migrate_from_sheets.py --source sheets --dry-run --snapshot-out route_snapshot.json
  # 2) 手元の xlsx（ファイル → ダウンロード → xlsx）や CSV のフォルダーから試す
  python tools/migrate_from_sheets.py --source xlsx --file data.xlsx --database-url sqlite:///t.db
  python tools/migrate_from_sheets.py --source csv --dir ./csv --dry-run   # <タブ名>.csv を読む
  # 3) 本番の移行
  python tools/migrate_from_sheets.py --source sheets --database-url postgresql+psycopg://...

安全の決まり:
  - 読み込み元は本番の ID だけ（--allow-non-production-source を付けたときだけ別の ID を許す）。旧 DB は常に止める。
  - 見出し（文字）と列の数を最初に確かめる。決まった見出しが無ければ止める。
  - 決まった値（enum）に無い値、読めない日時・数値があれば止める。
  - 同じ requestId（明細 ID・履歴 ID も）が重なっていたら止める。
  - SAMPLE- / TEST-EXP- の申請（とその明細・履歴）は移さず一覧に出す。
  - 移行先の DB の 8 表のどれかに行があれば止める。接続先が未設定なら始めない。
移行の後:
  - DB から読み直して件数・合計金額を照合する。
  - 全申請の「承認経路・現在の段・承認者」を JSON に書き出す（新しいアプリの判定と比べるため）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from app.services import sheet_codec as codec  # noqa: E402
from app.services.sheets import SheetsApi, SheetsError, decode_table, read_all_tables  # noqa: E402

EXCLUDED_PREFIXES = ("SAMPLE-", "TEST-EXP-")
ID_COLUMNS = {"requests": "request_id", "request_items": "item_id", "history": "history_id"}


class MigrationStop(RuntimeError):
    """移行を止める理由（安全装置）。"""


# ===== 読み込み元 =====

def read_from_sheets(spreadsheet_id: str, token_provider: Callable[[], str],
                     allow_non_production: bool = False) -> dict:
    if spreadsheet_id == codec.LEGACY_SPREADSHEET_ID:
        raise MigrationStop("旧 DB（2026-06-06 で止まったアーカイブ）は移行元にしません。")
    if spreadsheet_id != codec.PRODUCTION_SPREADSHEET_ID and not allow_non_production:
        raise MigrationStop(f"移行元が本番の ID ではありません: {spreadsheet_id}")
    return read_all_tables(SheetsApi(token_provider), spreadsheet_id)


def read_from_xlsx(path: str) -> dict:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    result = {}
    for table in codec.EXPORT_ORDER:
        sheet = codec.TABLES[table].sheet
        if sheet not in book.sheetnames:
            raise MigrationStop(f"タブ「{sheet}」がありません: {path}")
        values = [[("" if v is None else v) for v in row] for row in book[sheet].iter_rows(values_only=True)]
        result[table] = decode_table(table, values)
    return result


def read_from_csv_dir(directory: str) -> dict:
    result = {}
    for table in codec.EXPORT_ORDER:
        sheet = codec.TABLES[table].sheet
        path = Path(directory) / f"{sheet}.csv"
        if not path.exists():
            raise MigrationStop(f"CSV がありません: {path}")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            values = [row for row in csv.reader(handle)]
        result[table] = decode_table(table, values)
    return result


# ===== 確かめと仕分け =====

def find_duplicates(rows: list, key: str) -> list:
    counts = Counter(r[key] for r in rows)
    return sorted(k for k, n in counts.items() if n > 1)


def prepare(reads: dict) -> dict:
    """読んだ表を確かめ、移す行と報告を作る。止める理由があれば MigrationStop。"""
    for table, id_column in ID_COLUMNS.items():
        dup = find_duplicates(reads[table].rows, id_column)
        if dup:
            raise MigrationStop(f"{codec.TABLES[table].sheet}: 同じ {id_column} が重なっています: {', '.join(dup[:20])}")
        blank = [r for r in reads[table].rows if not r[id_column]]
        if blank:
            raise MigrationStop(f"{codec.TABLES[table].sheet}: {id_column} が空の行が {len(blank)} 行あります。")

    excluded = sorted(r["request_id"] for r in reads["requests"].rows
                      if r["request_id"].startswith(EXCLUDED_PREFIXES))
    excluded_set = set(excluded)

    def keep(row: dict) -> bool:
        rid = row.get("request_id", "")
        return rid not in excluded_set and not rid.startswith(EXCLUDED_PREFIXES)

    tables = {}
    excluded_counts = {}
    for table in codec.EXPORT_ORDER:
        rows = reads[table].rows
        if table in ("requests", "request_items", "history"):
            kept = [r for r in rows if keep(r)]
            excluded_counts[table] = len(rows) - len(kept)
            rows = kept
        tables[table] = rows

    request_ids = {r["request_id"] for r in tables["requests"]}
    orphans = {
        table: sorted({r["request_id"] for r in tables[table] if r["request_id"] not in request_ids})
        for table in ("request_items", "history")
    }
    warnings = [w for t in codec.EXPORT_ORDER for w in reads[t].warnings]
    for table in codec.EXPORT_ORDER:
        if reads[table].extra_headers:
            warnings.append(f"{codec.TABLES[table].sheet}: 決まりに無い見出し（移しません）: {', '.join(reads[table].extra_headers)}")
    for table, ids in orphans.items():
        if ids:
            warnings.append(f"{codec.TABLES[table].sheet}: 申請に無い申請 ID を指す行があります: {', '.join(ids[:20])}")
    return {"tables": tables, "excluded_request_ids": excluded, "excluded_counts": excluded_counts,
            "orphans": orphans, "warnings": warnings}


def summarize(tables: dict) -> dict:
    requests = tables["requests"]
    total = sum((r["total_amount"] or Decimal(0)) for r in requests)
    by_status = Counter(r["status"] for r in requests)
    amount_by_status: dict = {}
    for r in requests:
        amount_by_status[r["status"]] = amount_by_status.get(r["status"], Decimal(0)) + (r["total_amount"] or Decimal(0))
    in_review_steps = Counter(r["current_step"] for r in requests if r["status"] == "承認中")
    return {
        "counts": {t: len(rows) for t, rows in tables.items()},
        "totalAmount": codec.js_string(total),
        "statusCounts": dict(sorted(by_status.items())),
        "amountByStatus": {k: codec.js_string(v) for k, v in sorted(amount_by_status.items())},
        "inReviewStepCounts": dict(sorted(in_review_steps.items())),
        "itemAmountTotal": codec.js_string(sum((r["amount"] or Decimal(0)) for r in tables["request_items"])),
    }


def route_snapshot(tables: dict) -> list:
    """全申請の承認経路・現在の段・承認者（新しいアプリの判定と比べるため）。"""
    return [
        {
            "requestId": r["request_id"],
            "status": r["status"],
            "currentStep": r["current_step"],
            "currentApproverEmail": r["current_approver_email"],
            "currentApproverName": r["current_approver_name"],
            "route": r["route_json"],
            "department": r["department"],
            "applicantEmail": r["applicant_email"],
            "category": r["category"],
            "totalAmount": codec.js_string(r["total_amount"]),
            "amountWaived": r["amount_waived"],
        }
        for r in tables["requests"]
    ]


# ===== DB への書き込み（SQLAlchemy の表を読み取って使う。モデルには依存しない） =====

def insert_tables(engine: Any, tables: dict) -> dict:
    """表名 → dict のリストを 1 つのトランザクションで入れる。8 表のどれかに行があれば止める。

    戻り値: 表名 → DB に無いため落とした列の一覧（担当 F への依頼の材料）。
    """
    from sqlalchemy import MetaData, func, select

    metadata = MetaData()
    metadata.reflect(engine, only=lambda name, _m: name in tables)
    missing = [t for t in tables if t not in metadata.tables]
    if missing:
        raise MigrationStop(f"移行先の DB に表がありません: {', '.join(missing)}")
    dropped = {}
    with engine.begin() as conn:
        not_empty = {t: conn.execute(select(func.count()).select_from(metadata.tables[t])).scalar_one()
                     for t in tables}
        not_empty = {t: n for t, n in not_empty.items() if n}
        if not_empty:
            raise MigrationStop("移行先の DB が空ではありません（消してから書く道具は作りません）: "
                                + ", ".join(f"{t}={n}" for t, n in not_empty.items()))
        for table in codec.EXPORT_ORDER:
            rows = tables.get(table) or []
            target = metadata.tables[table]
            names = set(target.columns.keys())
            dropped[table] = sorted({k for r in rows for k in r if k not in names})
            if rows:
                conn.execute(target.insert(), [{k: v for k, v in r.items() if k in names} for r in rows])
    return dropped


def verify_in_db(engine: Any, tables: dict) -> list:
    """入れた後に DB から読み直し、件数と合計金額が合うか確かめる。合わない点の一覧を返す。"""
    from sqlalchemy import MetaData, func, select

    metadata = MetaData()
    metadata.reflect(engine, only=lambda name, _m: name in tables)
    problems = []
    with engine.connect() as conn:
        for table, rows in tables.items():
            n = conn.execute(select(func.count()).select_from(metadata.tables[table])).scalar_one()
            if n != len(rows):
                problems.append(f"{table}: 件数が違います（シート {len(rows)} / DB {n}）")
        req = metadata.tables["requests"]
        db_total = conn.execute(select(func.coalesce(func.sum(req.c.total_amount), 0))).scalar_one()
    sheet_total = sum((r["total_amount"] or Decimal(0)) for r in tables["requests"])
    if Decimal(str(db_total)) != sheet_total:
        problems.append(f"requests: 合計金額が違います（シート {sheet_total} / DB {db_total}）")
    return problems


# ===== 実行 =====

def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return codec.js_string(value)
    return str(value)


def run(reads: dict, engine: Optional[Any], dry_run: bool, snapshot_out: Optional[str] = None) -> dict:
    prepared = prepare(reads)
    report = {
        "summary": summarize(prepared["tables"]),
        "excludedRequestIds": prepared["excluded_request_ids"],
        "excludedCounts": prepared["excluded_counts"],
        "warnings": prepared["warnings"],
        "headerCounts": {t: reads[t].header_count for t in codec.EXPORT_ORDER},
        "dryRun": dry_run,
    }
    if snapshot_out:
        Path(snapshot_out).write_text(
            json.dumps(route_snapshot(prepared["tables"]), ensure_ascii=False, indent=1, default=_json_default),
            encoding="utf-8")
        report["snapshot"] = snapshot_out
    if not dry_run:
        if engine is None:
            raise MigrationStop("移行先の DB（--database-url か DATABASE_URL）が未設定です。")
        report["droppedColumns"] = {t: c for t, c in insert_tables(engine, prepared["tables"]).items() if c}
        report["verifyProblems"] = verify_in_db(engine, prepared["tables"])
    return report


def _token_provider() -> Callable[[], str]:
    try:  # 担当 F の委任トークン
        from app.google_delegate import get_access_token  # type: ignore

        return lambda: get_access_token(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    except ImportError:
        token = os.environ.get("SHEETS_ACCESS_TOKEN", "")
        if not token:
            raise MigrationStop("委任トークンを得られません（app.google_delegate が無く、SHEETS_ACCESS_TOKEN も未設定）。")
        return lambda: token


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="本番スプレッドシート → DB の移行（読むだけ）")
    parser.add_argument("--source", choices=["sheets", "xlsx", "csv"], required=True)
    parser.add_argument("--spreadsheet-id", default=codec.PRODUCTION_SPREADSHEET_ID)
    parser.add_argument("--allow-non-production-source", action="store_true")
    parser.add_argument("--file", help="xlsx のパス")
    parser.add_argument("--dir", help="<タブ名>.csv を置いたフォルダー")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true", help="確かめと報告だけ。DB に書かない")
    parser.add_argument("--snapshot-out", help="承認経路・現在の段・承認者の JSON の書き出し先")
    parser.add_argument("--report-out", help="報告の JSON の書き出し先")
    args = parser.parse_args(argv)

    try:
        engine = None
        if not args.dry_run:
            if not args.database_url:
                raise MigrationStop("移行先の DB（--database-url か DATABASE_URL）が未設定です。")
            from sqlalchemy import create_engine

            engine = create_engine(args.database_url)
        if args.source == "sheets":
            reads = read_from_sheets(args.spreadsheet_id, _token_provider(), args.allow_non_production_source)
        elif args.source == "xlsx":
            reads = read_from_xlsx(args.file)
        else:
            reads = read_from_csv_dir(args.dir)
        report = run(reads, engine, args.dry_run, args.snapshot_out)
    except (MigrationStop, codec.CodecError, SheetsError) as error:
        print(f"止めました: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, ensure_ascii=False, indent=1, default=_json_default)
    if args.report_out:
        Path(args.report_out).write_text(text, encoding="utf-8")
    print(text)
    return 1 if report.get("verifyProblems") else 0


if __name__ == "__main__":
    sys.exit(main())
