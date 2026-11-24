"""環境変数の読み込み。必須の値が欠けている・危ない組み合わせのときは起動しない（fail-closed）。

本番のスプレッドシート ID は固定値として持ち、ステージングの書き込み先がこれと同じなら起動を止める。
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.timeutil import parse_jst_until

# 本番（GAS 版が今使っている）スプレッドシート。ステージングからは読むだけ。
PROD_SPREADSHEET_ID_FIXED = "1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


def split_emails(value: str | None) -> list[str]:
    """GAS 版の splitEmails_ と同じ区切り（カンマ・改行・セミコロン）。小文字にして空を除く。"""
    return [e.strip().lower() for e in re.split(r"[,\n;]", value or "") if e.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_env: Literal["staging", "production"]
    database_url: str = Field(min_length=1)
    public_origin: str = "https://supplies.lineworks-local.net"

    google_oauth_client_id: str = Field(min_length=1)
    google_oauth_client_secret: str = Field(min_length=1)
    session_secret: str = Field(min_length=32)
    session_max_age_days: int = Field(default=14, ge=1, le=90)
    session_idle_hours: int = Field(default=12, ge=1, le=24 * 90)  # 使っていない時間の期限
    allowed_domain: str = "lineworks-local.info"
    bootstrap_admins: str = "imaizumi@lineworks-local.info"

    # 委任（Gmail・Drive・Sheets を imaizumi@ の権限で使う）
    fernet_key: str = Field(min_length=1)
    delegate_email: str = "imaizumi@lineworks-local.info"

    # 安全装置
    mail_redirect_to: str = ""
    prod_spreadsheet_id: str = PROD_SPREADSHEET_ID_FIXED
    prod_pdf_folder_id: str = ""
    staging_spreadsheet_id: str = ""
    staging_pdf_folder_id: str = ""

    # スプレッドシートの書き出し: この日時（日本時間 yyyy-MM-dd HH:mm）までは毎時、その後（空なら最初から）は毎晩だけ
    export_hourly_until: str = ""

    @field_validator("public_origin")
    @classmethod
    def _origin(cls, v: str) -> str:
        v = v.rstrip("/")
        if not v.startswith("https://"):
            raise ValueError("PUBLIC_ORIGIN は https:// で始めてください")
        return v

    @field_validator("allowed_domain", "delegate_email", "mail_redirect_to")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("export_hourly_until")
    @classmethod
    def _until(cls, v: str) -> str:
        parse_jst_until(v)  # 形が違えば起動しない
        return v.strip()

    @field_validator("fernet_key")
    @classmethod
    def _fernet(cls, v: str) -> str:
        try:
            Fernet(v.encode())
        except Exception as exc:  # noqa: BLE001 - 形式違いはすべて起動失敗にする
            raise ValueError("FERNET_KEY が Fernet の鍵の形式ではありません") from exc
        return v

    @model_validator(mode="after")
    def _safety(self) -> "Settings":
        if self.prod_spreadsheet_id != PROD_SPREADSHEET_ID_FIXED:
            raise ValueError("PROD_SPREADSHEET_ID は本番の ID から変えられません")
        if not self.bootstrap_admin_list:
            raise ValueError("BOOTSTRAP_ADMINS が空です")
        if not self.delegate_email.endswith("@" + self.allowed_domain):
            raise ValueError("DELEGATE_EMAIL は社内ドメインのアドレスにしてください")
        if self.app_env == "staging":
            missing = [
                name
                for name in ("mail_redirect_to", "staging_spreadsheet_id", "staging_pdf_folder_id", "prod_pdf_folder_id")
                if not getattr(self, name).strip()
            ]
            if missing:
                raise ValueError("ステージングに必要な値がありません: " + ", ".join(n.upper() for n in missing))
            if not _EMAIL_RE.match(self.mail_redirect_to):
                raise ValueError("MAIL_REDIRECT_TO がメールアドレスの形ではありません")
            if self.staging_spreadsheet_id.strip() == self.prod_spreadsheet_id:
                raise ValueError("STAGING_SPREADSHEET_ID が本番のスプレッドシートを指しています")
            if self.staging_pdf_folder_id.strip() == self.prod_pdf_folder_id.strip():
                raise ValueError("STAGING_PDF_FOLDER_ID が本番の PDF フォルダーを指しています")
        return self

    @property
    def bootstrap_admin_list(self) -> list[str]:
        return split_emails(self.bootstrap_admins)

    @property
    def is_staging(self) -> bool:
        return self.app_env == "staging"

    @property
    def oauth_redirect_uri(self) -> str:
        return self.public_origin + "/auth/callback"

    @property
    def delegate_redirect_uri(self) -> str:
        return self.public_origin + "/auth/google-delegate/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
