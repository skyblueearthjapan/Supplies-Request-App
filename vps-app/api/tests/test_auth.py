import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth, rpc
from app.models import Setting, UserSession

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_id_token(nonce, key=KEY, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": "1234567890",
        "iat": now,
        "exp": now + 600,
        "nonce": nonce,
        "email": "Tanaka@lineworks-local.info",
        "email_verified": True,
        "hd": "lineworks-local.info",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def fake_google(monkeypatch):
    monkeypatch.setattr(auth, "resolve_signing_key", lambda token: KEY.public_key())
    holder = {"claims": {}, "key": KEY, "calls": []}

    def _exchange(code, redirect_uri, verifier, settings):
        holder["calls"].append((code, redirect_uri, verifier))
        claims = {"nonce": holder["nonce"], **holder["claims"]}
        return {"id_token": make_id_token(claims.pop("nonce"), key=holder["key"], **claims)}

    monkeypatch.setattr(auth, "exchange_code", _exchange)
    return holder


def start_login(client, next_path=None):
    resp = client.get("/auth/login", params={"next": next_path} if next_path else None)
    assert resp.status_code == 302
    q = parse_qs(urlparse(resp.headers["location"]).query)
    return resp, {k: v[0] for k, v in q.items()}


def finish_login(client, fake_google, params, state=None):
    fake_google["nonce"] = params["nonce"]
    return client.get("/auth/callback", params={"code": "abc", "state": state or params["state"]})


def test_login_redirect_has_state_nonce_pkce_and_hd(client):
    resp, q = start_login(client)
    assert resp.headers["location"].startswith(auth.GOOGLE_AUTH_URL)
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "https://supplies.lineworks-local.net/auth/callback"
    assert q["hd"] == "lineworks-local.info"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) > 30 and len(q["nonce"]) > 30
    cookie = resp.headers["set-cookie"]
    assert auth.LOGIN_FLOW_COOKIE in cookie and "HttpOnly" in cookie and "Secure" in cookie
    assert "samesite=lax" in cookie.lower()


def test_successful_login_returns_to_request_detail(client, db, fake_google):
    _, q = start_login(client, "/?requestId=REQ-20261125000000-000001")
    resp = finish_login(client, fake_google, q)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/?requestId=REQ-20261125000000-000001"
    set_cookie = ";".join(resp.headers.get_list("set-cookie"))
    assert auth.SESSION_COOKIE in set_cookie
    row = db.query(UserSession).one()
    assert row.email == "tanaka@lineworks-local.info" and row.name == "tanaka"
    # PKCE の verifier がトークン交換に渡っている
    assert fake_google["calls"][0][2]


def test_state_mismatch_is_rejected(client, fake_google, db):
    _, q = start_login(client)
    resp = finish_login(client, fake_google, q, state="wrong-state")
    assert resp.status_code == 403
    assert db.query(UserSession).count() == 0


def test_callback_without_flow_cookie_is_rejected(client, fake_google, db):
    fake_google["nonce"] = "n"
    resp = client.get("/auth/callback", params={"code": "abc", "state": "s"})
    assert resp.status_code == 403
    assert db.query(UserSession).count() == 0


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "other-nonce"},
        {"aud": "someone-else"},
        {"azp": "someone-else"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200},
        {"email_verified": False},
        {"email_verified": None},
        {"hd": "gmail.com"},
        {"hd": None},
        {"email": "tanaka@gmail.com"},
        {"sub": None},
    ],
)
def test_bad_id_token_claims_are_rejected(client, fake_google, db, claims):
    fake_google["claims"] = claims
    _, q = start_login(client)
    resp = finish_login(client, fake_google, q)
    assert resp.status_code == 403
    assert db.query(UserSession).count() == 0


def test_bad_signature_is_rejected(client, fake_google, db):
    fake_google["key"] = OTHER_KEY
    _, q = start_login(client)
    assert finish_login(client, fake_google, q).status_code == 403
    assert db.query(UserSession).count() == 0


def test_google_error_param_is_rejected(client, db):
    _, q = start_login(client)
    resp = client.get("/auth/callback", params={"error": "access_denied", "state": q["state"]})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/?requestId=REQ-1", "/?requestId=REQ-1"),
        ("//evil.example.com/", "/"),
        ("https://evil.example.com/", "/"),
        ("/\\evil.example.com", "/"),
        ("/auth/login", "/"),
        ("", "/"),
        (None, "/"),
        ("/a\nb", "/"),
    ],
)
def test_safe_next(value, expected):
    assert auth.safe_next(value) == expected


def test_rpc_requires_login_and_logout_revokes(client, db, login_as):
    resp = client.post("/api/rpc/getBootstrap", json=[])
    assert resp.status_code == 401
    assert resp.json() == {
        "ok": False,
        "error": "ログインの有効期限が切れました。もう一度ログインしてください。",
        "login": "/auth/login",
    }
    login_as("Someone@lineworks-local.info")
    assert client.post("/api/rpc/getBootstrap", json=[]).status_code == 501  # ログイン済み・未実装
    saved = dict(client.cookies)
    assert client.post("/auth/logout").status_code == 200
    assert db.query(UserSession).count() == 0
    for k, v in saved.items():  # 古いクッキーを持ち続けても使えない
        client.cookies.set(k, v)
    assert client.post("/api/rpc/getBootstrap", json=[]).status_code == 401


def test_logout_is_post_only(client, db, login_as):
    login_as("someone@lineworks-local.info")
    assert client.get("/auth/logout").status_code == 405
    assert db.query(UserSession).count() == 1


def test_idle_session_expires_and_use_extends_it(client, db, login_as, settings, monkeypatch):
    from datetime import timedelta

    from app.timeutil import now_utc

    rpc.register("getBootstrap", lambda ctx: "ok")
    login_as("someone@lineworks-local.info")
    row = db.query(UserSession).one()
    # ログイン直後の期限は 12 時間後（ログインから 14 日より短い）
    assert abs((row.expires_at - row.created_at) - timedelta(hours=settings.session_idle_hours)) < timedelta(seconds=5)
    assert settings.session_idle_hours == 12

    # 11 時間後に使うと、そこから 12 時間に延びる
    later = now_utc() + timedelta(hours=11)
    monkeypatch.setattr(auth, "now_utc", lambda: later)
    assert client.post("/api/rpc/getBootstrap", json=[]).status_code == 200
    db.expire_all()
    assert db.query(UserSession).one().expires_at >= later + timedelta(hours=12) - timedelta(seconds=5)

    # その後 13 時間使わなければ切れる
    monkeypatch.setattr(auth, "now_utc", lambda: later + timedelta(hours=13))
    assert client.post("/api/rpc/getBootstrap", json=[]).status_code == 401


def test_session_never_outlives_max_age(settings):
    from datetime import datetime, timedelta, timezone

    created = datetime(2026, 11, 1, tzinfo=timezone.utc)
    near_end = created + timedelta(days=settings.session_max_age_days) - timedelta(hours=1)
    assert auth._session_expiry(created, near_end, settings) == created + timedelta(days=settings.session_max_age_days)


def test_revoke_sessions_removes_all_sessions_of_one_email(db, settings):
    from fastapi.responses import Response

    for email in ("a@lineworks-local.info", "a@lineworks-local.info", "b@lineworks-local.info"):
        auth.create_session(db, Response(), email, settings)
    assert auth.revoke_sessions(db, " A@lineworks-local.info ") == 2
    db.commit()
    assert [s.email for s in db.query(UserSession).all()] == ["b@lineworks-local.info"]
    assert auth.revoke_sessions(db, "") == 0


def test_tampered_session_cookie_is_rejected(client, login_as):
    login_as("someone@lineworks-local.info")
    value = client.cookies.get(auth.SESSION_COOKIE)
    client.cookies.set(auth.SESSION_COOKIE, value[:-2] + ("AA" if not value.endswith("AA") else "BB"))
    assert client.post("/api/rpc/getBootstrap", json=[]).status_code == 401


def test_is_admin_uses_settings_and_bootstrap_and_never_everyone(db, settings):
    assert auth.is_admin(db, "imaizumi@lineworks-local.info", settings)
    assert not auth.is_admin(db, "someone@lineworks-local.info", settings)  # 一覧が空でも全員にしない
    db.add(Setting(key="adminEmails", value="Boss@lineworks-local.info, other@lineworks-local.info"))
    db.commit()
    assert auth.is_admin(db, "boss@lineworks-local.info", settings)
    assert auth.is_admin(db, "IMAIZUMI@lineworks-local.info", settings)
    assert not auth.is_admin(db, "", settings)


def test_user_name_is_local_part():
    u = auth.user_from_email(" Tanaka.Taro@lineworks-local.info ")
    assert u.email == "tanaka.taro@lineworks-local.info" and u.name == "tanaka.taro"
