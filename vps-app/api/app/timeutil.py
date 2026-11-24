"""時刻と ID。DB には UTC の時刻を入れ、画面へは日本時間の文字列で返す（serialize.py）。"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_jst() -> datetime:
    return datetime.now(JST)


def today_jst() -> date:
    """申請日の既定（日本時間の今日。GAS 版の朝 9 時前は前日になる癖は直す）。"""
    return now_jst().date()


def new_id(prefix: str, at: datetime | None = None) -> str:
    """GAS 版 createId_ と同じ形: PREFIX-yyyyMMddHHmmss-6桁（日本時間）。重複は DB の一意制約で防ぐ。"""
    moment = (at or now_utc()).astimezone(JST)
    return f"{prefix}-{moment:%Y%m%d%H%M%S}-{secrets.randbelow(1_000_000):06d}"


def parse_jst_until(value: str | None) -> datetime | None:
    """「この日時まで」の設定（日本時間）を datetime にする。空なら None、形が違えば ValueError。

    yyyy-MM-dd HH:mm[:ss]（T 区切りも可）はその時刻まで。yyyy-MM-dd だけならその日の終わりまで（翌日 0 時の手前）。
    """
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=JST)
        except ValueError:
            pass
    try:
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=JST)
    except ValueError:
        raise ValueError(f"日本時間の日時（yyyy-MM-dd HH:mm）の形ではありません: {text}") from None
    return day + timedelta(days=1)
