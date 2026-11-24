"""PDF のドライブ保存（imaizumi@ の委任トークンで Drive API）。GAS 版 Helpers.gs の getPdfFolder* と Pdf.gs の保存部分の移植。

- 置き場: 設定 pdfFolderId のフォルダの中に、申請日（yyyy-MM-dd）のサブフォルダ（無ければ作る）。今と同じ。
- 作り直したら古い PDF はゴミ箱へ。取消・却下でもゴミ箱へ（30 日は戻せる）。
- staging は本番のフォルダに書かない（app.safety）:
  * 置き場は safety.pdf_root_folder_id()（staging は STAGING_PDF_FOLDER_ID。設定の pdfFolderId は本番から移した値なので使わない）。
  * フォルダの作成・アップロードの直前に safety.assert_writable_drive_folder() を祖先つきで呼ぶ。
  * ゴミ箱へ移すのも、staging の置き場の中のファイルだけ（移したデータの pdfFileId は本番の PDF を指すため）。
- GAS 版はフォルダ ID が空なら新しく作っていたが、VPS 版は作らずに止める（safety が UnsafeWriteError）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from app import safety
from app.services.mailpdf_common import sanitize_text, today_jst

log = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(RuntimeError):
    pass


@dataclass
class DriveFile:
    id: str
    url: str


def date_folder_name(date_str: Any) -> str:
    """申請日ごとのサブフォルダ名。形が違えば今日（日本時間）。"""
    name = sanitize_text(date_str)[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        name = today_jst()
    return name


class DriveClient:
    """Drive API の最小限の操作。credentials_provider は委任の Credentials を返す関数。"""

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
        s = self._session()
        q = f"'{parent_id}' in parents and name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
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


class PdfStorage:
    """申請の PDF の保存・ゴミ箱。jobs から使う。app_settings は app.config.Settings（None なら get_settings()）。"""

    def __init__(self, client: DriveClient, app_settings: Any = None):
        self.client = client
        self.app_settings = app_settings

    def save(self, settings: dict[str, Any], request_date: Any, file_name: str, data: bytes) -> DriveFile:
        root = safety.pdf_root_folder_id(sanitize_text(settings.get("pdfFolderId"), 256), self.app_settings)
        folder = self.client.find_or_create_folder(root, date_folder_name(request_date))
        safety.assert_writable_drive_folder(folder, ancestor_ids=[root], settings=self.app_settings)
        return self.client.upload_pdf(folder, file_name, data)

    def trash(self, settings: dict[str, Any], file_id: str) -> bool:
        """ゴミ箱へ。staging では staging の置き場の中（親か祖父母）のファイルだけ。失敗は記録して False。"""
        if not file_id:
            return False
        try:
            parents = self.client.parents(file_id)
            grand = [gp for p in parents for gp in self.client.parents(p)]
            try:
                for p in parents or [""]:
                    safety.assert_writable_drive_folder(p, ancestor_ids=grand, settings=self.app_settings)
            except safety.UnsafeWriteError as unsafe:
                log.warning("drive.trash skipped (%s): %s", unsafe, file_id)
                return False
            return self.client.trash(file_id)
        except Exception as error:  # GAS 版と同じく、消せなくても本体の処理は止めない
            log.warning("drive.trash failed: %s: %s", file_id, error)
            return False

    def download(self, file_id: str) -> bytes:
        return self.client.download(file_id)
