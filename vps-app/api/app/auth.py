"""Google ログイン（OpenID Connect の Authorization Code ＋ PKCE）とセッション。

検証すること: state（クッキーと照合）、nonce、id_token の署名（Google の JWKS）、iss、aud（と azp）、
exp・iat、email_verified、hd（社内ドメイン）、メールのドメイン。

セッション: クッキーには署名付きの乱数を入れ、DB（sessions 表）には その SHA-256 を置く。
ログアウトで DB の行を消すので、クッキーが残っていても使えなくなる。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings, split_emails
from app.db import get_db
from app.models import Setting, UserSession
from app.timeutil import now_utc

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

SESSION_COOKIE = "__Host-chozouhin_session"
LOGIN_FLOW_COOKIE = "__Host-chozouhin_login"
FLOW_MAX_AGE_SECONDS = 600
CLOCK_SKEW_SECONDS = 60

LOGIN_SCOPES = "openid email profile"

router = APIRouter()


# ===== 利用者 =====


@dataclass(frozen=True)
class CurrentUser:
    email: str
    name: str


def user_from_email(email: str) -> CurrentUser:
    """GAS 版 getCurrentUser_ と同じ: メールは小文字、名前は @ より前。"""
    normalized = (email or "").strip().lower()[:256]
    return CurrentUser(email=normalized, name=normalized.split("@")[0])


class AuthRequired(Exception):
    """ログインしていない・切れた。main.py が 401 の JSON にする。"""


class LoginRejected(Exception):
    """Google から戻ったが、受け入れられない（ドメイン違い・検証失敗など）。"""


def is_admin(db: Session, email: str, settings: Settings | None = None) -> bool:
    """設定 adminEmails ∪ BOOTSTRAP_ADMINS。一覧が空でも全員を管理者にはしない（GAS 版の癖は直す）。"""
    s = settings or get_settings()
    target = (email or "").strip().lower()
    if not target:
        return False
    row = db.get(Setting, "adminEmails")
    admins = set(split_emails(row.value if row else "")) | set(s.bootstrap_admin_list)
    return target in admins


# ===== 署名・ハッシュ =====


def _serializer(settings: Settings, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=salt)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


# ===== セッション =====


def create_session(db: Session, response: Response, email: str, settings: Settings) -> CurrentUser:
    user = user_from_email(email)
    now = now_utc()
    db.execute(delete(UserSession).where(UserSession.expires_at < now))
    token = secrets.token_urlsafe(32)
    max_age = settings.session_max_age_days * 86400
    db.add(
        UserSession(
            token_hash=_token_hash(token),
            email=user.email,
            name=user.name,
            created_at=now,
            expires_at=now + timedelta(seconds=max_age),
        )
    )
    db.commit()
    _set_cookie(response, SESSION_COOKIE, _serializer(settings, "session").dumps(token), max_age)
    return user


def _session_token(request: Request, settings: Settings) -> str | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        token = _serializer(settings, "session").loads(raw, max_age=settings.session_max_age_days * 86400)
    except BadSignature:
        return None
    return token if isinstance(token, str) else None


def get_optional_user(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> CurrentUser | None:
    token = _session_token(request, settings)
    if not token:
        return None
    row = db.execute(
        select(UserSession).where(UserSession.token_hash == _token_hash(token), UserSession.expires_at > now_utc())
    ).scalar_one_or_none()
    if row is None:
        return None
    return user_from_email(row.email)


def require_user(user: CurrentUser | None = Depends(get_optional_user)) -> CurrentUser:
    if user is None:
        raise AuthRequired()
    return user


# ===== 戻り先 =====


def safe_next(value: str | None) -> str:
    """ログイン後の戻り先。同じサイトの中の相対パスだけ（?requestId= などはそのまま保つ）。"""
    text = (value or "").strip()
    if (
        not text
        or len(text) > 2000
        or not text.startswith("/")
        or text.startswith("//")
        or text.startswith("/\\")
        or "\\" in text
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)
        or text.startswith("/auth/")
    ):
        return "/"
    return text


# ===== OAuth の往復（ログインと委任で共用） =====


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def begin_oauth(
    *,
    settings: Settings,
    cookie_name: str,
    redirect_uri: str,
    scope: str,
    extra_params: dict[str, str],
    flow_data: dict[str, Any],
) -> RedirectResponse:
    """state・nonce・PKCE を作り、署名付きの短命クッキーに入れて Google へ送る。"""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "hd": settings.allowed_domain,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
        **extra_params,
    }
    response = RedirectResponse(GOOGLE_AUTH_URL + "?" + urlencode(params), status_code=302)
    payload = {"state": state, "nonce": nonce, "verifier": verifier, **flow_data}
    _set_cookie(response, cookie_name, _serializer(settings, cookie_name).dumps(payload), FLOW_MAX_AGE_SECONDS)
    return response


def load_oauth_flow(request: Request, settings: Settings, cookie_name: str, state: str | None) -> dict[str, Any]:
    """クッキーの flow を読み、state を照合する。合わなければ LoginRejected。"""
    raw = request.cookies.get(cookie_name)
    if not raw:
        raise LoginRejected("ログインの手続きの期限が切れました。もう一度お試しください。")
    try:
        flow = _serializer(settings, cookie_name).loads(raw, max_age=FLOW_MAX_AGE_SECONDS)
    except BadSignature as exc:
        raise LoginRejected("ログインの手続きの期限が切れました。もう一度お試しください。") from exc
    expected = flow.get("state") if isinstance(flow, dict) else None
    if not expected or not state or not hmac.compare_digest(str(expected), str(state)):
        raise LoginRejected("ログインの手続きが正しくありません。もう一度お試しください。")
    return flow


def exchange_code(code: str, redirect_uri: str, code_verifier: str, settings: Settings) -> dict[str, Any]:
    """認可コード → トークン（id_token、委任のときは refresh_token も）。"""
    resp = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": settings.google_oauth_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        log.warning("token exchange failed: %s", resp.status_code)
        raise LoginRejected("Google からの応答を確かめられませんでした。もう一度お試しください。")
    return resp.json()


_jwks_client: jwt.PyJWKClient | None = None


def resolve_signing_key(id_token: str) -> Any:
    """id_token の kid に合う Google の公開鍵（JWKS はキャッシュする）。テストでは差し替える。"""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client.get_signing_key_from_jwt(id_token).key


def verify_id_token(id_token: str, *, nonce: str, settings: Settings) -> dict[str, Any]:
    """署名・iss・aud・azp・exp・iat・nonce・email_verified・hd・メールのドメインを確かめ、claims を返す。"""
    if not id_token:
        raise LoginRejected("Google からログインの情報を受け取れませんでした。")
    try:
        key = resolve_signing_key(id_token)
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=settings.google_oauth_client_id,
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["iss", "aud", "exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as exc:
        log.info("id_token rejected: %s", exc)
        raise LoginRejected("ログインの情報を確かめられませんでした。もう一度お試しください。") from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise LoginRejected("ログインの情報を確かめられませんでした。もう一度お試しください。")
    azp = claims.get("azp")
    if azp is not None and azp != settings.google_oauth_client_id:
        raise LoginRejected("ログインの情報を確かめられませんでした。もう一度お試しください。")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), str(nonce)):
        raise LoginRejected("ログインの手続きが正しくありません。もう一度お試しください。")
    if claims.get("email_verified") not in (True, "true"):
        raise LoginRejected("メールアドレスが確認済みの Google アカウントでログインしてください。")
    domain = settings.allowed_domain
    email = str(claims.get("email", "")).strip().lower()
    if claims.get("hd") != domain or not email.endswith("@" + domain):
        raise LoginRejected(f"社内（{domain}）の Google アカウントでログインしてください。")
    claims["email"] = email
    return claims


# ===== 画面（短い HTML） =====


def message_page(title: str, message: str, status_code: int, link: str = "/", link_text: str = "トップへ") -> HTMLResponse:
    body = (
        "<!doctype html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title></head>"
        "<body style=\"font-family:sans-serif;max-width:40em;margin:3em auto;padding:0 16px;line-height:1.7\">"
        f"<h1 style=\"font-size:1.3em\">{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        f"<p><a href=\"{html.escape(link)}\">{html.escape(link_text)}</a></p></body></html>"
    )
    return HTMLResponse(body, status_code=status_code, headers={"Cache-Control": "no-store"})


# ===== 経路 =====


@router.get("/auth/login")
def login(next: str | None = None, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    return begin_oauth(
        settings=settings,
        cookie_name=LOGIN_FLOW_COOKIE,
        redirect_uri=settings.oauth_redirect_uri,
        scope=LOGIN_SCOPES,
        extra_params={"prompt": "select_account", "access_type": "online"},
        flow_data={"next": safe_next(next)},
    )


@router.get("/auth/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    try:
        flow = load_oauth_flow(request, settings, LOGIN_FLOW_COOKIE, state)
        if error or not code:
            raise LoginRejected("ログインが取り消されました。")
        tokens = exchange_code(code, settings.oauth_redirect_uri, flow["verifier"], settings)
        claims = verify_id_token(tokens.get("id_token", ""), nonce=flow["nonce"], settings=settings)
    except LoginRejected as exc:
        page = message_page("ログインできませんでした", str(exc), 403, "/auth/login", "もう一度ログインする")
        _clear_cookie(page, LOGIN_FLOW_COOKIE)
        return page

    response = RedirectResponse(safe_next(flow.get("next")), status_code=302)
    _clear_cookie(response, LOGIN_FLOW_COOKIE)
    create_session(db, response, claims["email"], settings)
    return response


@router.api_route("/auth/logout", methods=["GET", "POST"])
def logout(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    token = _session_token(request, settings)
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _token_hash(token)))
        db.commit()
    page = message_page("ログアウトしました", "画面を閉じてかまいません。", 200, "/auth/login", "もう一度ログインする")
    _clear_cookie(page, SESSION_COOKIE)
    return page
