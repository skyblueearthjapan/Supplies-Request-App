"""PDF のドライブ保存（imaizumi@ の委任トークンで Drive API）。GAS 版 Helpers.gs の getPdfFolder* と Pdf.gs の保存部分の移植。

- 置き場: 設定 pdfFolderId のフォルダの中に、申請日（yyyy-MM-dd）のサブフォルダ（無ければ作る）。今と同じ。
- 作り直したら古い PDF はゴミ箱へ。取消・却下でもゴミ箱へ（30 日は戻せる）。
- staging は本番のフォルダに書かない:
  * 置き場は環境変数 STAGING_PDF_FOLDER_ID（設定の pdfFolderId は本番から移したものなので使わない）。
  * 書く前に safety.assert_writable_drive_folder() で本番のフォルダでないことを確かめる。
  * ゴミ箱へ移すのも、staging の置き場の中にあるファイルだけ（移したデータの pdfFileId は本番の PDF を指すため）。
- GAS 版はフォルダ ID が空なら新しく作っていたが、VPS 版は作らずに止める（行き先の分からない書き込みをしない）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from .mailpdf_common import MailPdfConfig, sanitize_text, today_jst

log = logging.getLogger(__name__)

try:  # 担当 F の安全装置。まだ無いときは同じ名前の仮の関数を使う。
    from app.safety import assert_writable_drive_folder  # type: ignore
except Exception:  # pragma: no cover - F の成果が入れば使われない
    import os

    def assert_writable_drive_folder(folder_id: str) -> None:  # type: ignore[no-redef]
        """仮の安全装置（担当 F の app.safety ができたら置き換わる）。

        staging では、書き込み先が STAGING_PDF_FOLDER_ID と同じで、本番の PDF フォルダ（PRODUCTION_PDF_FOLDER_ID）
        でないことを確かめる。違えば例外で止める。
        """
        env = (os.environ.get("APP_ENV") or "staging").strip().lower()
        prod = (os.environ.get("PRODUCTION_PDF_FOLDER_ID") or "").strip()
        if not folder_id:
            raise RuntimeError("PDF の保存先フォルダが決まっていません。")
        if env != "production":
            if prod and folder_id == prod:
                raise RuntimeError("staging から本番の PDF フォルダへは書き込めません。")
            staging = (os.environ.get("STAGING_PDF_FOLDER_ID") or "").strip()
            if not staging or folder_id != staging:
                raise RuntimeError("staging の PDF の保存先は STAGING_PDF_FOLDER_ID だけです。")


DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    pass


@dataclass
class DriveFile:
    id: str
    url: str


def pdf_root_folder_id(config: MailPdfConfig, settings: dict[str, Any]) -> str:
    """PDF の置き場（親フォルダ）。staging は STAGING_PDF_FOLDER_ID、production は設定の pdfFolderId。"""
    folder_id = config.staging_pdf_folder_id if not config.is_production else sanitize_text(settings.get("pdfFolderId"), 256)
    if not folder_id:
        raise DriveError("PDF の保存先フォルダ（pdfFolderId / STAGING_PDF_FOLDER_ID）が設定されていません。")
    return folder_id


def date_folder_name(date_str: Any) -> str:
    """申請日ごとのサブフォルダ名。形が違えば今日（日本時間）。"""
    name = sanitize_text(date_str)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        name = today_jst()
    return name


class DriveClient:
    """Drive API の最小限の操作。credentials は google_delegate.get_delegate_credentials() の戻り値。"""

    def __init__(self, credentials_provider: Callable[[], Any], timeout: float = 60.0,
                 session_factory: Callable[[Any], Any] | None = None):
        self._credentials_provider = credentials_provider
        self._timeout = timeout
        self._session_factory = session_factory

    def _session(self):
        creds = self._credentials_provider()
        if self._session_factory is not None:
            return self._session_factory(creds)
        from google.auth.transport.requests import AuthorizedSession

        return AuthorizedSession(creds)

    @staticmethod
    def _check(resp, what: str):
        if resp.status_code >= 400:
            raise DriveError(f"Drive API の{what}に失敗しました: HTTP {resp.status_code} {resp.text[:300]}")
        return resp

    def find_or_create_folder(self, parent_id: str, name: str) -> str:
        assert_writable_drive_folder(parent_id)
        s = self._session()
        q = (f"'{parent_id}' in parents and name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false")
        resp = self._check(s.get(f"{DRIVE_API}/files", params={"q": q, "fields": "files(id,name)", "pageSize": 10,
                                                               "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
                                 timeout=self._timeout), "フォルダの検索")
        files = resp.json().get("files", [])
        if files:
            return files[0]["id"]
        resp = self._check(s.post(f"{DRIVE_API}/files", params={"supportsAllDrives": "true", "fields": "id"},
                                  json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                                  timeout=self._timeout), "フォルダの作成")
        return resp.json()["id"]

    def upload_pdf(self, folder_id: str, name: str, data: bytes) -> DriveFile:
        assert_writable_drive_folder_tree(folder_id, self)
        boundary = "chozouhin-pdf-boundary"
        meta = json.dumps({"name": name, "parents": [folder_id], "mimeType": "application/pdf"}, ensure_ascii=False)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n").encode("utf-8") + data + f"\r\n--{boundary}--".encode()
        resp = self._check(self._session().post(
            UPLOAD_API, params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,webViewLink"},
            data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"}, timeout=self._timeout),
            "PDF の保存")
        j = resp.json()
        return DriveFile(id=j["id"], url=j.get("webViewLink") or f"https://drive.google.com/file/d/{j['id']}/view?usp=drivesdk")

    def parents(self, file_id: str) -> list[str]:
        resp = self._session().get(f"{DRIVE_API}/files/{file_id}", params={"fields": "parents", "supportsAllDrives": "true"},
                                   timeout=self._timeout)
        if resp.status_code == 404:
            return []
        self._check(resp, "親フォルダの確認")
        return list(resp.json().get("parents", []))

    def trash(self, file_id: str) -> bool:
        """ゴミ箱へ移す。無い・消えているときは何もしない（GAS 版と同じく無視）。"""
        resp = self._session().patch(f"{DRIVE_API}/files/{file_id}", params={"supportsAllDrives": "true"},
                                     json={"trashed": True}, timeout=self._timeout)
        if resp.status_code == 404:
            return False
        self._check(resp, "ゴミ箱への移動")
        return True

    def download(self, file_id: str) -> bytes:
        resp = self._check(self._session().get(f"{DRIVE_API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                                               timeout=self._timeout), "PDF の読み出し")
        return resp.content


def assert_writable_drive_folder_tree(folder_id: str, client: DriveClient) -> None:
    """日付のサブフォルダに書く前に、その親（置き場）も安全装置に通す。"""
    parents = client.parents(folder_id)
    if not parents:
        assert_writable_drive_folder(folder_id)
        return
    for p in parents:
        assert_writable_drive_folder(p)


class PdfStorage:
    """申請の PDF の保存・ゴミ箱。jobs から使う。"""

    def __init__(self, client: DriveClient, config: MailPdfConfig):
        self.client = client
        self.config = config

    def save(self, settings: dict[str, Any], request_date: Any, file_name: str, data: bytes) -> DriveFile:
        root = pdf_root_folder_id(self.config, settings)
        assert_writable_drive_folder(root)
        folder = self.client.find_or_create_folder(root, date_folder_name(request_date))
        return self.client.upload_pdf(folder, file_name, data)

    def trash(self, settings: dict[str, Any], file_id: str) -> bool:
        """ゴミ箱へ。staging では staging の置き場の中のファイルだけ（それ以外は記録して飛ばす）。失敗は記録して False。"""
        if not file_id:
            return False
        try:
            if not self.config.is_production:
                root = pdf_root_folder_id(self.config, settings)
                parents = self.client.parents(file_id)
                grand = [gp for p in parents for gp in self.client.parents(p)]
                if root not in parents and root not in grand:
                    log.warning("drive.trash skipped on staging (not under staging folder): %s", file_id)
                    return False
            return self.client.trash(file_id)
        except Exception as error:  # GAS 版と同じく、消せなくても本体の処理は止めない
            log.warning("drive.trash failed: %s: %s", file_id, error)
            return False

    def download(self, file_id: str) -> bytes:
        return self.client.download(file_id)
