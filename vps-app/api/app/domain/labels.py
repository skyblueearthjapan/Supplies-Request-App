"""Constants.gs のうち、業務と画面で使う定数（enum の値そのものは app.constants）。"""
from __future__ import annotations

from app.constants import CATEGORIES, STEPS

DEFAULT_THRESHOLD = 100000
# 消費税率。単価・金額はすべて税抜で保存し、消費税・税込は表示時に算出する。
TAX_RATE = 0.10

CATEGORY_LABELS = {
    "メカ": "メカ",
    "電気": "電気",
    "一般不明": "一般・不明",
}

STATUS_LABELS = {
    "承認中": "承認中",
    "差戻し": "差戻し",
    "完了": "完了",
    "取消": "取消",
}

STEP_LABELS = {
    "上席": "上席",
    "購買見積": "購買（見積）",
    "総務部長": "総務部長",
    "社長": "社長",
    "購買手配": "購買",
    "申請者": "申請者",
    "完了済": "完了",
}

ACTION_LABELS = {
    "申請": "申請",
    "承認": "承認",
    "差戻": "差戻し",
    "再申請": "再申請",
    "更新": "更新",
    "完了処理": "完了",
    "社長決裁へ": "社長決裁へ",
    "金額確定": "金額確定",
    "金額不要至急": "金額不要・至急",
    "PDF作成": "PDF作成",
    "取消処理": "取消",
    "前段戻し": "前段へ戻す",
    "却下": "却下",
    "明細分離": "明細を分離",
}

REASONS = [
    {"code": "A", "label": "4月以降も都度購入を認めた消耗品（ウエス、オイル等）"},
    {"code": "B", "label": "対象工番が特殊案件で新規に購入しなければ対応できないもの"},
    {"code": "C", "label": "年度末に購入した消耗品を使い切った場合"},
    {"code": "D", "label": "工具等を修理する場合"},
    {"code": "E", "label": "工具等を修理するよりも新規で購入したほうが安価な場合"},
    {"code": "F", "label": "社長が特別に認可した場合"},
]

UNIT_OPTIONS = ["個", "本", "枚", "台", "箱", "ケース", "セット", "袋", "缶", "巻", "束", "組", "双", "kg", "L", "m"]
DEFAULT_UNIT = "個"
# セレクトの「その他（自由入力）」を表す番兵値。実データとしては保存しない。
UNIT_OTHER = "__other__"

# 明細の部分差戻し（申請の分割）を許可するステップ。
SPLITTABLE_STEPS = (STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT)

# 経路（標準）。社長決裁は見積確定でしきい値以上のとき総務部長と購買手配の間に入る。
STANDARD_ROUTE = [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING]
PRESIDENT_ROUTE = [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT, STEPS.PURCHASING]

# 全画面承認（代理記録）で操作できる段（GAS @94 の assertKioskTarget_）。
KIOSK_STEPS = (STEPS.SUPERVISOR, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT)

DEFAULT_SETTINGS = [
    {"key": "thresholdAmount", "value": str(DEFAULT_THRESHOLD),
     "description": "社長決裁が必要になる合計金額（税抜・この金額以上で社長決裁）"},
    {"key": "pdfFolderId", "value": "",
     "description": "完了PDFを保存するGoogle DriveフォルダID。空欄なら自動作成。"},
    {"key": "adminEmails", "value": "imaizumi@lineworks-local.info",
     "description": "管理者メールアドレス。カンマ区切り。空欄の間は全員を初期管理者として扱う。"},
    {"key": "enableEmailNotifications", "value": "true",
     "description": "承認依頼・差戻し・完了メールを送信する。"},
]

CATEGORY_VALUES_ORDERED = (CATEGORIES.MECH, CATEGORIES.ELEC, CATEGORIES.GENERAL)
