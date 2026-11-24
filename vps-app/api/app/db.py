"""DB の接続とセッション。本番は PostgreSQL 16、テストは SQLite（メモリー）。

PostgreSQL だけの機能（行ロック）は SQLAlchemy の with_for_update() で書く。SQLite では
SQLAlchemy が FOR UPDATE を出さないので、同じコードがテストでもそのまま動く。
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from functools import lru_cache

from sqlalchemy import DateTime, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """timestamptz。書くときは UTC にそろえ、読むときは必ず時差付き（UTC）で返す。

    SQLite は時差を持たないため、時差のない値は UTC として扱う。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError("UTCDateTime には datetime を渡してください")
        if value.tzinfo is None:
            raise ValueError("時差のない datetime は保存できません（timeutil.now_utc() を使う）")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    from app.config import get_settings

    return make_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=True)


def get_db() -> Iterator[Session]:
    """FastAPI の依存。commit は呼び出し側（rpc.py など）が行う。"""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
