from urllib.parse import parse_qs, urlparse

import pytest

from app import auth, google_delegate
from app.google_delegate import DelegateNotAuthorized, get_delegate_credentials, get_delegate_status
from app.models import GoogleCredential

from test_auth import KEY, make_id_token

ADMIN = "imaizumi@lineworks-local.info"
ALL_SCOPES = " ".join(google_delegate.DELEGATE_SCOPES)


@pytest.fixture
def fake_google(monkeypatch):
    monkeypatch.setattr(auth, "resolve_signing_key", lambda token: KEY.public_key())
    holder = {"email": ADMIN, "refresh_token": "1//refresh-token", "scope": ALL_SCOPES}

    def _exchange(code, redirect_uri, verifier, settings):
        holder["redirect_uri"] = redirect_uri
        tokens = {"id_token": make_id_token(holder["nonce"], email=holder["email"]), "scope": holder["scope"]}
        if holder["refresh_token"]:
            tokens["refresh_token"] = holder["refresh_token"]
        return tokens

    monkeypatch.setattr(auth, "exchange_code", _exchange)
    return holder


def start(client):
    resp = client.get("/auth/google-delegate/start")
    assert resp.status_code == 302, resp.text
    return {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}


def test_not_authorized_status(db, settings):
    assert get_delegate_status(db) == {"authorized": False, "accountEmail": "", "grantedBy": "", "grantedAt": "", "scopes": []}
    with pytest.raises(DelegateNotAuthorized):
        get_delegate_credentials(db, settings)


def test_start_requires_admin(client, login_as):
    assert client.get("/auth/google-delegate/start").status_code == 302  # 未ログインはログインへ
    login_as("someone@lineworks-local.info")
    assert client.get("/auth/google-delegate/start").status_code == 403


def test_start_requests_offline_consent_for_imaizumi(client, login_as):
    login_as(ADMIN)
    q = start(client)
    assert q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["login_hint"] == ADMIN
    assert q["redirect_uri"] == "https://supplies.lineworks-local.net/auth/google-delegate/callback"
    for scope in ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/drive",
                  "https://www.googleapis.com/auth/spreadsheets", "openid", "email"):
        assert scope in q["scope"].split()


def test_callback_stores_encrypted_token(client, db, login_as, fake_google, settings):
    login_as(ADMIN)
    q = start(client)
    fake_google["nonce"] = q["nonce"]
    resp = client.get("/auth/google-delegate/callback", params={"code": "c", "state": q["state"]})
    assert resp.status_code == 200, resp.text
    row = db.get(GoogleCredential, "delegate")
    assert row.account_email == ADMIN and row.granted_by_email == ADMIN
    assert "1//refresh-token" not in row.refresh_token_encrypted
    status = get_delegate_status(db)
    assert status["authorized"] is True and status["accountEmail"] == ADMIN and len(status["grantedAt"]) == 19
    creds = get_delegate_credentials(db, settings)
    assert creds.refresh_token == "1//refresh-token"
    assert creds.client_id == settings.google_oauth_client_id


@pytest.mark.parametrize(
    "change",
    [
        {"email": "someone@lineworks-local.info"},  # 委任は imaizumi@ だけ
        {"refresh_token": ""},
        {"scope": "openid email https://www.googleapis.com/auth/drive"},  # Gmail を外された
    ],
)
def test_callback_rejects_bad_grants(client, db, login_as, fake_google, change):
    login_as(ADMIN)
    q = start(client)
    fake_google.update(change)
    fake_google["nonce"] = q["nonce"]
    resp = client.get("/auth/google-delegate/callback", params={"code": "c", "state": q["state"]})
    assert resp.status_code == 403
    assert db.get(GoogleCredential, "delegate") is None


def test_wrong_fernet_key_is_reported(db, settings):
    from conftest import make_settings
    from cryptography.fernet import Fernet

    google_delegate.save_delegate_token(
        db, account_email=ADMIN, refresh_token="t", scopes=ALL_SCOPES, granted_by=ADMIN, settings=settings
    )
    db.commit()
    other = make_settings(fernet_key=Fernet.generate_key().decode())
    with pytest.raises(DelegateNotAuthorized):
        get_delegate_credentials(db, other)
