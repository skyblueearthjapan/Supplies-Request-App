"""画面から呼ぶ関数（RPC）の登録と呼び出し。`POST /api/rpc/{fn}`、本文は引数の JSON 配列。

応答（実装仕様 2 章）:
- 成功      200 {"ok": true, "result": ...}
- 業務エラー 400 {"ok": false, "error": "GAS 版と同じ日本語の文"}（RpcError を raise する）
- 未ログイン 401 {"ok": false, "error": ..., "login": "/auth/login"}（main.py）
- 知らない関数 404、まだ作っていない関数 501、想定外のエラー 500

登録（担当 W）: app/domain/register.py に `def register_all(register): ...` を置き、
`register("approveRequest", approve_request)` のように 22 関数を差し込む。
関数の形は `def approve_request(ctx: RpcContext, requestId, comment='', kiosk=False, returnItems=None)`。
引数は GAS 版と同じ並びで、位置引数として渡す。戻り値は JSON にできる値（serialize.py で作る）。
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import CurrentUser, is_admin, require_user
from app.config import Settings, get_settings
from app.db import get_db
from app.models import IdempotencyKey
from app.serialize import fmt_date, fmt_datetime, fmt_number
from app.timeutil import now_utc

log = logging.getLogger(__name__)

RPC_FUNCTIONS: tuple[str, ...] = (
    "getBootstrap",
    "getTabCounts",
    "getRequests",
    "getRequestDetail",
    "createRequest",
    "updateRequestDraft",
    "updateRequestInReview",
    "resubmitRequest",
    "approveRequest",
    "returnRequest",
    "confirmQuote",
    "confirmExpedited",
    "arrangeComplete",
    "recallStep",
    "deleteRequest",
    "generateRequestPdf",
    "recordPresidentDecision",
    "syncDeptSupervisorsNow",
    "syncWorkersNow",
    "saveApproverMaster",
    "saveNotificationRecipients",
    "saveSettings",
)
READ_ONLY_FUNCTIONS = frozenset({"getBootstrap", "getTabCounts", "getRequests", "getRequestDetail"})

IDEMPOTENCY_HEADER = "X-Idempotency-Key"
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

ERR_SHAPE = "呼び出しの形が正しくありません。"
ERR_ARGS = "引数の数が正しくありません。"
ERR_INTERNAL = "サーバーでエラーが起きました。時間をおいてもう一度お試しください。"
ERR_IN_PROGRESS = "同じ操作を処理しています。少し待ってから画面を読み込み直してください。"
ERR_KEY_REUSED = "同じ操作キーが別の操作に使われています。画面を読み込み直してください。"


class RpcError(Exception):
    """業務エラー。message は画面にそのまま出る（GAS 版の throw new Error('...') と同じ文にする）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class RpcNotImplemented(Exception):
    pass


@dataclass
class RpcContext:
    """業務関数に渡す。db のトランザクションは呼び出し後に rpc.py が commit する（失敗なら rollback）。"""

    db: Session
    user: CurrentUser
    settings: Settings
    request: Request | None = None
    fn: str = ""
    _is_admin: bool | None = field(default=None, repr=False)

    @property
    def is_admin(self) -> bool:
        if self._is_admin is None:
            self._is_admin = is_admin(self.db, self.user.email, self.settings)
        return self._is_admin


Handler = Callable[..., Any]


def _not_implemented(ctx: RpcContext, *args: Any) -> Any:
    raise RpcNotImplemented()


_registry: dict[str, Handler] = {}


def reset_registry() -> None:
    """すべてを「まだ作っていない（501）」に戻す。"""
    _registry.clear()
    _registry.update({name: _not_implemented for name in RPC_FUNCTIONS})


reset_registry()


def register(name: str, handler: Handler | None = None):
    """関数を登録する。22 関数以外の名前は ValueError。デコレーターとしても使える。"""
    if name not in RPC_FUNCTIONS:
        raise ValueError(f"公開できない関数名です: {name}")

    def _do(h: Handler) -> Handler:
        _registry[name] = h
        return h

    return _do(handler) if handler is not None else _do


def get_handler(name: str) -> Handler | None:
    return _registry.get(name)


def is_implemented(name: str) -> bool:
    return _registry.get(name, _not_implemented) is not _not_implemented


def load_domain_handlers() -> bool:
    """app.domain.register があれば register_all(register) を呼ぶ。ないときは全部 501 のまま。"""
    if importlib.util.find_spec("app.domain") is None or importlib.util.find_spec("app.domain.register") is None:
        return False
    module = importlib.import_module("app.domain.register")
    module.register_all(register)
    return True


# ===== JSON =====


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return fmt_datetime(value)
    if isinstance(value, date):
        return fmt_date(value)
    if isinstance(value, Decimal):
        return fmt_number(value)
    raise TypeError(f"JSON にできない値です: {type(value).__name__}")


def _dumps(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def json_response(body: dict[str, Any] | str, status_code: int = 200) -> Response:
    content = body if isinstance(body, str) else _dumps(body)
    return Response(
        content=content,
        status_code=status_code,
        media_type="application/json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def error_response(message: str, status_code: int) -> Response:
    return json_response({"ok": False, "error": message}, status_code)


# ===== 呼び出し =====


def _call(ctx: RpcContext, handler: Handler, args: list[Any]) -> Any:
    try:
        bound = inspect.signature(handler).bind(ctx, *args)
    except TypeError as exc:
        raise RpcError(ERR_ARGS) from exc
    return handler(*bound.args, **bound.kwargs)


def _replay(db: Session, user_email: str, key: str, fn: str) -> Response | None:
    row = db.get(IdempotencyKey, (user_email, key))
    if row is None:
        return None
    if row.fn != fn:
        return error_response(ERR_KEY_REUSED, 400)
    if not row.completed:
        return error_response(ERR_IN_PROGRESS, 409)
    return json_response(row.response_json)


def purge_idempotency_keys(db: Session, older_than_hours: int = 24) -> int:
    """古い二重押し対策のキーを消す（worker の定期処理から呼ぶ）。消した件数を返す。commit は呼び出し側。"""
    from datetime import timedelta

    from sqlalchemy import delete

    cutoff = now_utc() - timedelta(hours=older_than_hours)
    result = db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff))
    return int(result.rowcount or 0)


def dispatch(fn: str, args: list[Any], *, db: Session, user: CurrentUser, settings: Settings,
             request: Request | None = None, idempotency_key: str | None = None) -> Response:
    handler = _registry.get(fn)
    if handler is None:
        return error_response("その操作はありません。", 404)
    ctx = RpcContext(db=db, user=user, settings=settings, request=request, fn=fn)
    use_key = bool(idempotency_key) and fn not in READ_ONLY_FUNCTIONS
    key_row: IdempotencyKey | None = None

    try:
        if use_key:
            replay = _replay(db, user.email, idempotency_key, fn)  # type: ignore[arg-type]
            if replay is not None:
                db.rollback()
                return replay
            key_row = IdempotencyKey(user_email=user.email, key=idempotency_key, fn=fn, created_at=now_utc())
            db.add(key_row)
            try:
                # PostgreSQL では、同じキーを処理中の別の呼び出しがあれば、その確定まで待ってから重複になる
                db.flush()
            except IntegrityError:
                db.rollback()
                replay = _replay(db, user.email, idempotency_key, fn)  # type: ignore[arg-type]
                db.rollback()
                return replay or error_response(ERR_IN_PROGRESS, 409)

        result = _call(ctx, handler, args)
        body = _dumps({"ok": True, "result": result})
        if key_row is not None:
            key_row.response_json = body
            key_row.completed = True
        db.commit()
        return json_response(body)
    except RpcError as exc:
        db.rollback()
        return error_response(exc.message, 400)
    except RpcNotImplemented:
        db.rollback()
        return error_response(f"この機能はまだ用意されていません。（{fn}）", 501)
    except Exception:
        db.rollback()
        log.exception("rpc %s failed", fn)
        return error_response(ERR_INTERNAL, 500)


router = APIRouter()


@router.post("/api/rpc/{fn}")
async def call_rpc(
    fn: str,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    user: CurrentUser = Depends(require_user),
) -> Response:
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != settings.public_origin:
        return error_response("この画面からは操作できません。", 403)
    if fn not in _registry:
        return error_response("その操作はありません。", 404)

    raw = await request.body()
    if raw.strip():
        try:
            args = json.loads(raw)
        except ValueError:
            return error_response(ERR_SHAPE, 400)
    else:
        args = []
    if not isinstance(args, list):
        return error_response(ERR_SHAPE, 400)

    key = request.headers.get(IDEMPOTENCY_HEADER)
    if key is not None and not _IDEMPOTENCY_KEY_RE.match(key):
        return error_response(ERR_SHAPE, 400)

    return await run_in_threadpool(
        dispatch, fn, args, db=db, user=user, settings=settings, request=request, idempotency_key=key
    )
