"""安全装置（実装仕様 4 章）。ステージングから本番のスプレッドシート・PDF フォルダーへ書かせない。

使い方（担当 M・D）:
- 書き込む直前に assert_writable_spreadsheet(id) / assert_writable_drive_folder(id, ancestor_ids=...) を呼ぶ。
- 書き込み先は export_spreadsheet_id() / pdf_root_folder_id(settings の pdfFolderId) で決める。
  ステージングでは設定の値（本番から移した値）を使わず、STAGING_* を返す。
"""
from __future__ import annotations

from collections.abc import Iterable

from app.config import Settings, get_settings


class UnsafeWriteError(RuntimeError):
    """本番の書き込み先にステージングから書こうとした。処理を止める（握りつぶさない）。"""


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


# どの環境からも書き込まないスプレッドシート（読むだけ）
LEGACY_SPREADSHEET_ID = "1xCkpAIB8wPq49mzTwy6WNj3i485eV9oGwnzE-7aREQo"  # 旧 DB（アーカイブ）
WORKER_MASTER_SPREADSHEET_ID = "1iu5HoaknlW1W1HheeYv0jqcRq-aY0SyEE2seQd2pHkQ"  # 作業員マスタ（Constants.gs）
DEPT_APPROVER_SPREADSHEET_ID = "1Knx_kaQMZZams65J1oeSDaBeWUt8XXanNe94XSAHKFQ"  # DeptApprovers（Constants.gs）
NEVER_WRITABLE_SPREADSHEET_IDS = frozenset({
    LEGACY_SPREADSHEET_ID, WORKER_MASTER_SPREADSHEET_ID, DEPT_APPROVER_SPREADSHEET_ID,
})


def assert_writable_spreadsheet(spreadsheet_id: str, settings: Settings | None = None) -> str:
    """書き込んでよいスプレッドシートか（許可リスト）。

    - 旧 DB・作業員マスタ・DeptApprovers はどの環境からも書き込まない。
    - ステージングは STAGING_SPREADSHEET_ID だけ、本番は本番の ID だけに書ける。ほかは UnsafeWriteError。
    """
    s = _settings(settings)
    sid = (spreadsheet_id or "").strip()
    if not sid:
        raise UnsafeWriteError("書き込み先のスプレッドシート ID が空です")
    if sid in NEVER_WRITABLE_SPREADSHEET_IDS:
        raise UnsafeWriteError("読むだけのスプレッドシート（旧 DB・作業員マスタ・DeptApprovers）へは書き込めません")
    allowed = s.staging_spreadsheet_id.strip() if s.is_staging else s.prod_spreadsheet_id
    if sid != allowed:
        if s.is_staging and sid == s.prod_spreadsheet_id:
            raise UnsafeWriteError("ステージングから本番のスプレッドシートへは書き込めません")
        raise UnsafeWriteError("書き込みを許可していないスプレッドシートです")
    return sid


def assert_writable_drive_folder(
    folder_id: str, ancestor_ids: Iterable[str] = (), settings: Settings | None = None
) -> str:
    """書き込んでよいドライブのフォルダーか。

    日付のサブフォルダーは ID が別になるため、親（たどれる限りの祖先）の ID も ancestor_ids に渡す。
    ステージングでは、自分か祖先が本番の PDF フォルダーなら、またはステージングのフォルダーの中でなければ
    UnsafeWriteError。本番では、自分か祖先が PROD_PDF_FOLDER_ID でなければ（またはステージングのフォルダーなら）
    UnsafeWriteError。
    """
    s = _settings(settings)
    fid = (folder_id or "").strip()
    if not fid:
        raise UnsafeWriteError("書き込み先のフォルダー ID が空です")
    chain = [fid] + [a.strip() for a in ancestor_ids if a and a.strip()]
    prod = s.prod_pdf_folder_id.strip()
    if not s.is_staging:
        # 本番は本番の PDF フォルダー（PROD_PDF_FOLDER_ID）の中にだけ書く（設定 pdfFolderId の取り違えで別の場所に書かない）
        if s.staging_pdf_folder_id.strip() and s.staging_pdf_folder_id.strip() in chain:
            raise UnsafeWriteError("本番からステージング用の PDF フォルダーへは書き込めません")
        if prod not in chain:
            raise UnsafeWriteError("本番では本番の PDF フォルダー（PROD_PDF_FOLDER_ID）の中にだけ書き込めます")
        return fid
    if prod and prod in chain:
        raise UnsafeWriteError("ステージングから本番の PDF フォルダーへは書き込めません")
    if s.staging_pdf_folder_id.strip() not in chain:
        raise UnsafeWriteError("ステージングではステージング用の PDF フォルダーの中にだけ書き込めます")
    return fid


def export_spreadsheet_id(settings: Settings | None = None) -> str:
    """申請データの書き出し先。本番は今のスプレッドシート、ステージングはそのコピー。"""
    s = _settings(settings)
    target = s.staging_spreadsheet_id if s.is_staging else s.prod_spreadsheet_id
    return assert_writable_spreadsheet(target, s)


def pdf_root_folder_id(setting_value: str, settings: Settings | None = None) -> str:
    """PDF の保存先（日付フォルダーの親）。本番は設定 pdfFolderId、ステージングは STAGING_PDF_FOLDER_ID。"""
    s = _settings(settings)
    target = s.staging_pdf_folder_id if s.is_staging else setting_value
    return assert_writable_drive_folder(target, settings=s)


def forced_mail_redirect(settings: Settings | None = None) -> str:
    """ステージングでは全メールの送り先（MAIL_REDIRECT_TO）。本番は ''（管理画面のテスト転送は別に M が扱う）。"""
    s = _settings(settings)
    return s.mail_redirect_to if s.is_staging else ""
