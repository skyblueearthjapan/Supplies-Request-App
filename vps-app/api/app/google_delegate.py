"""委任トークン: imaizumi@ の権限で Gmail 送信・Drive・Sheets を使う（決定事項 9-2・9-3）。

流れ: 管理者が /auth/google-delegate/start を開く → Google の同意画面（imaizumi@ でログイン）→
/auth/google-delegate/callback でリフレッシュトークンを Fernet で暗号化して google_credentials 表へ保存。
担当 M・D は get_delegate_credentials(db) で google.oauth2.credentials.Credentials を受け取って API を呼ぶ。
"""
from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app import auth
from app.auth import (
    GOOGLE_TOKEN_URL,
    CurrentUser,
    LoginRejected,
    _clear_cookie,
    begin_oauth,
    get_optional_user,
    is_admin,
    load_oauth_flow,
    message_page,
    safe_next,
    verify_id_token,
)
from app.config import Settings, get_settings
from app.db import get_db
from app.models import GoogleCredential
from app.serialize import fmt_datetime
from app.timeutil import now_utc

DELEGATE_NAME = "delegate"
DELEGATE_FLOW_COOKIE = "__Host-chozouhin_delegate"
DELEGATE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.send",
    # 送信が時間切れになったとき、送る前に決めた Message-ID で送信済みかを確かめる（ヘッダーだけ。本文は読めない）
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)
# 同意画面で外されると困るもの（openid・email は id_token で確かめる）
REQUIRED_API_SCOPES = DELEGATE_SCOPES[2:]

router = APIRouter()


class DelegateNotAuthorized(RuntimeError):
    """委任がまだ許可されていない・鍵が合わず読めない。メール・ドライブの仕事は失敗として記録する。"""


def _fernet(settings: Settings) -> Fernet:
    return Fernet(settings.fernet_key.encode())


def encrypt_token(token: str, settings: Settings) -> str:
    return _fernet(settings).encrypt(token.encode()).decode()


def decrypt_token(value: str, settings: Settings) -> str:
    try:
        return _fernet(settings).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise DelegateNotAuthorized("委任トークンを読めません（FERNET_KEY が変わった可能性）。管理画面から許可し直してください。") from exc


def save_delegate_token(
    db: Session, *, account_email: str, refresh_token: str, scopes: str, granted_by: str, settings: Settings
) -> GoogleCredential:
    now = now_utc()
    row = db.get(GoogleCredential, DELEGATE_NAME)
    encrypted = encrypt_token(refresh_token, settings)
    if row is None:
        row = GoogleCredential(name=DELEGATE_NAME, granted_at=now, updated_at=now, refresh_token_encrypted=encrypted)
        db.add(row)
    row.account_email = account_email
    row.refresh_token_encrypted = encrypted
    row.scopes = scopes
    row.granted_by_email = granted_by
    row.granted_at = now
    row.updated_at = now
    db.flush()
    return row


def get_delegate_credentials(db: Session, settings: Settings | None = None):
    """委任アカウントの Credentials（google.oauth2.credentials.Credentials）。最初の API 呼び出しで自動更新される。

    許可がなければ DelegateNotAuthorized。
    """
    from google.oauth2.credentials import Credentials

    s = settings or get_settings()
    row = db.get(GoogleCredential, DELEGATE_NAME)
    if row is None:
        raise DelegateNotAuthorized("Google の委任がまだ許可されていません。管理画面から許可してください。")
    return Credentials(
        token=None,
        refresh_token=decrypt_token(row.refresh_token_encrypted, s),
        token_uri=GOOGLE_TOKEN_URL,
        client_id=s.google_oauth_client_id,
        client_secret=s.google_oauth_client_secret,
        scopes=list(REQUIRED_API_SCOPES),
    )


def get_delegate_status(db: Session) -> dict[str, Any]:
    """管理画面用: 許可済みか・誰のアカウントか・誰がいつ許可したか。"""
    row = db.get(GoogleCredential, DELEGATE_NAME)
    if row is None:
        return {"authorized": False, "accountEmail": "", "grantedBy": "", "grantedAt": "", "scopes": []}
    return {
        "authorized": True,
        "accountEmail": row.account_email,
        "grantedBy": row.granted_by_email,
        "grantedAt": fmt_datetime(row.granted_at),
        "scopes": [s for s in row.scopes.split() if s],
    }


@router.get("/auth/google-delegate/start")
def delegate_start(
    request: Request,
    user: CurrentUser | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    if user is None:
        return RedirectResponse("/auth/login", status_code=302)
    if not is_admin(db, user.email, settings):
        return message_page("許可できません", "管理者のみ実行できます。", 403)
    return begin_oauth(
        settings=settings,
        cookie_name=DELEGATE_FLOW_COOKIE,
        redirect_uri=settings.delegate_redirect_uri,
        scope=" ".join(DELEGATE_SCOPES),
        extra_params={"access_type": "offline", "prompt": "consent", "login_hint": settings.delegate_email},
        flow_data={"by": user.email, "next": safe_next(request.query_params.get("next"))},
    )


@router.get("/auth/google-delegate/callback")
def delegate_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    user: CurrentUser | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        flow = load_oauth_flow(request, settings, DELEGATE_FLOW_COOKIE, state)
        if user is None or user.email != flow.get("by") or not is_admin(db, user.email, settings):
            raise LoginRejected("許可を始めた管理者のログインが確かめられません。もう一度お試しください。")
        if error or not code:
            raise LoginRejected("許可が取り消されました。")
        tokens = auth.exchange_code(code, settings.delegate_redirect_uri, flow["verifier"], settings)
        claims = verify_id_token(tokens.get("id_token", ""), nonce=flow["nonce"], settings=settings)
        if claims["email"] != settings.delegate_email:
            raise LoginRejected(f"許可は {settings.delegate_email} のアカウントで行ってください。")
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise LoginRejected("Google から更新用の鍵を受け取れませんでした。もう一度お試しください。")
        granted = set(str(tokens.get("scope", "")).split())
        missing = [s for s in REQUIRED_API_SCOPES if s not in granted]
        if missing:
            raise LoginRejected("同意画面ですべての項目（Gmail・ドライブ・スプレッドシート）にチェックを入れてください。")
    except LoginRejected as exc:
        page = message_page("Google の許可ができませんでした", str(exc), 403)
        _clear_cookie(page, DELEGATE_FLOW_COOKIE)
        return page

    save_delegate_token(
        db,
        account_email=claims["email"],
        refresh_token=refresh_token,
        scopes=" ".join(sorted(granted)),
        granted_by=user.email,
        settings=settings,
    )
    db.commit()
    page = message_page(
        "Google の許可を保存しました",
        f"{claims['email']} の権限でメール送信・ドライブ・スプレッドシートを使います。",
        200,
        flow.get("next") or "/",
        "アプリへ戻る",
    )
    _clear_cookie(page, DELEGATE_FLOW_COOKIE)
    return page
