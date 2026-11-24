"""FastAPI の入口。/api/rpc/{fn}、/auth/*、/healthz。

起動時に設定を読み（欠けていれば起動しない）、業務関数（app.domain.register）を登録する。
起動: `uvicorn --factory app.main:create_app`
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, google_delegate, rpc
from app.config import get_settings
from app.db import get_db

log = logging.getLogger(__name__)

LOGIN_URL = "/auth/login"


def create_app(load_handlers: bool = True) -> FastAPI:
    settings = get_settings()  # fail-closed: 必須の値が欠けていればここで止まる
    logging.basicConfig(level=logging.INFO)
    log.info("starting chozouhin api (APP_ENV=%s)", settings.app_env)

    app = FastAPI(title="chozouhin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(auth.AuthRequired)
    async def _auth_required(request: Request, exc: auth.AuthRequired) -> Response:
        return rpc.json_response(
            {"ok": False, "error": "ログインの有効期限が切れました。もう一度ログインしてください。", "login": LOGIN_URL},
            401,
        )

    @app.get("/healthz")
    def healthz(db: Session = Depends(get_db)) -> Response:
        try:
            db.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001
            log.exception("healthz: db check failed")
            return rpc.json_response({"ok": False}, 503)
        return rpc.json_response({"ok": True, "env": settings.app_env})

    app.include_router(auth.router)
    app.include_router(google_delegate.router)
    app.include_router(rpc.router)

    if load_handlers:
        rpc.load_domain_handlers()
    return app
