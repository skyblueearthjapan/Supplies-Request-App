"""GAS 版 Constants.gs の enum の値（セルに入る日本語の値）。DB の CHECK 制約と業務ロジックで共用する。

表示ラベル（*_LABELS）や REASONS などの業務用の定数は domain 側に置く。
"""
from __future__ import annotations

APP_NAME = "貯蔵品購入申請"
APP_VERSION = "1.0.0"  # requests.version 列に入る値（更新回数ではない。更新回数は row_version）
TIME_ZONE = "Asia/Tokyo"


class STATUS:
    IN_REVIEW = "承認中"
    RETURNED = "差戻し"
    COMPLETED = "完了"
    CANCELLED = "取消"


class STEPS:
    SUPERVISOR = "上席"
    PURCHASING_QUOTE = "購買見積"
    GENERAL_MANAGER = "総務部長"
    PRESIDENT = "社長"
    PURCHASING = "購買手配"
    APPLICANT = "申請者"
    DONE = "完了済"


class ACTION:
    SUBMIT = "申請"
    APPROVE = "承認"
    RETURN = "差戻"
    RESUBMIT = "再申請"
    UPDATE = "更新"
    COMPLETE = "完了処理"
    ESCALATE = "社長決裁へ"  # 古い履歴に残る値（今のコードは書かない）
    QUOTE = "金額確定"
    EXPEDITE = "金額不要至急"
    PDF_GENERATE = "PDF作成"
    CANCEL = "取消処理"
    RECALL = "前段戻し"
    REJECT = "却下"
    SPLIT_OUT = "明細分離"


class CATEGORIES:
    MECH = "メカ"
    ELEC = "電気"
    GENERAL = "一般不明"


class RECIPIENT_TYPES:
    GENERAL_AFFAIRS = "総務部"
    PURCHASING = "購買"
    PURCHASING_ELEC = "電気購買"


def _values(cls: type) -> tuple[str, ...]:
    return tuple(v for k, v in vars(cls).items() if not k.startswith("_"))


STATUS_VALUES = _values(STATUS)
STEP_VALUES = _values(STEPS)
ACTION_VALUES = _values(ACTION)
CATEGORY_VALUES = _values(CATEGORIES)
RECIPIENT_TYPE_VALUES = _values(RECIPIENT_TYPES)
SEND_AS_VALUES = ("TO", "CC")

# 後回しの仕事（jobs.state）
JOB_STATES = ("pending", "running", "done", "failed", "skipped")
