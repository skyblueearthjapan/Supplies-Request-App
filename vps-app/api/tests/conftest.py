"""テストの土台。SQLite（メモリー）に models の表を作り、FastAPI を https://testserver で動かす。

使い方（担当 W・M・D）:
- `db` フィクスチャ: 空の表がそろった Session。
- `client` フィクスチャ: TestClient（業務関数は登録しない。rpc.register で差し込む）。
- `login(client, db, email)`: そのメールでログインした状態のクッキーを client に付ける。
- `settings` フィクスチャ: テスト用の Settings（APP_ENV=staging）。
PostgreSQL だけの機能（行ロック with_for_update）は SQLite では何もしないので、同じコードがそのまま動く。
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

# app を import する前に環境変数をそろえる（config は起動時に必須値を確かめる）
TEST_ENV = {
    "APP_ENV": "staging",
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "PUBLIC_ORIGIN": "https://supplies.lineworks-local.net",
    "GOOGLE_OAUTH_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
    "SESSION_SECRET": "test-session-secret-0123456789abcdef0123456789",
    "ALLOWED_DOMAIN": "lineworks-local.info",
    "BOOTSTRAP_ADMINS": "imaizumi@lineworks-local.info",
    "FERNET_KEY": Fernet.generate_key().decode(),
    "MAIL_REDIRECT_TO": "imaizumi@lineworks-local.info",
    "STAGING_SPREADSHEET_ID": "staging-sheet-id",
    "STAGING_PDF_FOLDER_ID": "staging-folder-id",
    "PROD_PDF_FOLDER_ID": "prod-folder-id",
}
for _k, _v in TEST_ENV.items():
    os.environ[_k] = _v

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app import models as _models  # noqa: E402,F401 - すべての表を読み込む
from app import rpc  # noqa: E402
from app.auth import create_session  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.db import Base, get_db, make_engine  # noqa: E402
from app.main import create_app  # noqa: E402


def make_settings(**overrides) -> Settings:
    values = {k.lower(): v for k, v in TEST_ENV.items()}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def _test_database_url() -> str:
    """既定は SQLite（メモリー）。TEST_DATABASE_URL で PostgreSQL の結合テストにする。

    表を作って消すので、DB 名に test を含まない URL は拒む（本番の DB を消さないため）。
    """
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        return "sqlite+pysqlite:///:memory:"
    dbname = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in dbname.lower():
        raise RuntimeError("TEST_DATABASE_URL の DB 名には test を含めてください（表を消すため）")
    return url


@pytest.fixture
def engine():
    eng = make_engine(_test_database_url())
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory) -> Session:
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def app(session_factory, settings):
    rpc.reset_registry()
    application = create_app(load_handlers=False)

    def _db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()

    application.dependency_overrides[get_db] = _db
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    rpc.reset_registry()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def login(client: TestClient, db: Session, email: str, settings: Settings | None = None) -> None:
    """email でログインしたセッションを作り、クッキーを client に付ける。"""
    from fastapi.responses import Response

    response = Response()
    create_session(db, response, email, settings or make_settings())
    for header in response.headers.getlist("set-cookie"):
        name, _, rest = header.partition("=")
        client.cookies.set(name, rest.split(";", 1)[0])


@pytest.fixture
def login_as(client, db, settings):
    """login_as('someone@lineworks-local.info') でその人としてログインする。"""
    return lambda email: login(client, db, email, settings)
