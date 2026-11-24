"""メール・PDF・jobs のテストの共通データ（担当 M）。

マスタはシートの列名（camelCase）で書く。値の形はシートと同じ（有効は '' / 'true' / 'false' など）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.services.mailpdf_common import Masters  # noqa: E402

ORIGIN = "https://supplies.lineworks-local.net"


def approver_rules():
    return [
        {  # 全社の既定（'*'）
            "department": "*", "applicantEmail": "", "supervisorEmail": "", "supervisorName": "",
            "generalManagerEmail": "gm1@lineworks-local.info, gm2@lineworks-local.info",
            "generalManagerName": "辻野 一郎（総務部長）", "generalManagerTitle": "部長",
            "presidentEmail": "pres@lineworks-local.info", "presidentName": "社長 太郎", "presidentTitle": "",
            "purchasingEmail": "mech@lineworks-local.info", "purchasingName": "五十嵐（購買）", "purchasingTitle": "係長",
            "purchasingElecEmail": "elec@lineworks-local.info", "purchasingElecName": "電田 次郎", "purchasingElecTitle": "",
            "active": "",
        },
        {  # 部署の行（役職が空なら '*' で補う）
            "department": "製造部", "applicantEmail": "", "supervisorEmail": "old-sup@lineworks-local.info",
            "supervisorName": "旧上席", "supervisorTitle": "課長",
            "generalManagerEmail": "", "generalManagerName": "", "generalManagerTitle": "",
            "presidentEmail": "", "presidentName": "", "presidentTitle": "",
            "purchasingEmail": "", "purchasingName": "", "purchasingTitle": "",
            "purchasingElecEmail": "", "purchasingElecName": "", "purchasingElecTitle": "",
            "active": "true",
        },
    ]


def dept_supervisors():
    return [
        {"department": "製造部", "email": "sup1@lineworks-local.info", "name": "上田 一郎", "active": "", "title": "課長",
         "notifyEmail": "sup1-phone@lineworks-local.info"},
        {"department": "製造部", "email": "sup2@lineworks-local.info", "name": "", "active": "true", "title": "",
         "notifyEmail": "sup2@lineworks-local.info"},
        {"department": "製造部", "email": "sup3@lineworks-local.info", "name": "無効 三郎", "active": "false", "title": "",
         "notifyEmail": ""},
        {"department": "*", "email": "star-sup@lineworks-local.info", "name": "全社 上席", "active": "", "title": "",
         "notifyEmail": ""},
    ]


def recipients_rows():
    return [
        {"type": "総務部", "email": "ga1@lineworks-local.info", "name": "総務 花子 様", "active": "", "sendAs": "TO"},
        {"type": "総務部", "email": "ga2@lineworks-local.info", "name": "", "active": "true", "sendAs": "CC"},
        {"type": "購買", "email": "mech@lineworks-local.info", "name": "五十嵐 様", "active": "", "sendAs": "TO"},
        {"type": "購買", "email": "mech-cc@lineworks-local.info", "name": "購買控え", "active": "", "sendAs": "CC"},
        {"type": "電気購買", "email": "elec@lineworks-local.info", "name": "電田 様", "active": "", "sendAs": "TO"},
        {"type": "電気購買", "email": "ga2@lineworks-local.info", "name": "", "active": "", "sendAs": "TO"},  # 総務の Cc と重複
        {"type": "電気購買", "email": "off@lineworks-local.info", "name": "無効", "active": "false", "sendAs": "TO"},
        {"type": "購買", "email": "applicant@lineworks-local.info", "name": "申請者も購買", "active": "", "sendAs": "CC"},
        {"type": "購買", "email": "", "name": "空", "active": "", "sendAs": "TO"},
    ]


def make_masters(**overrides):
    data = {
        "approver_rules": approver_rules(),
        "dept_supervisors": dept_supervisors(),
        "recipients": recipients_rows(),
        "settings": {"enableEmailNotifications": "true", "thresholdAmount": "100000",
                     "adminEmails": "imaizumi@lineworks-local.info", "pdfFolderId": "PROD_FOLDER"},
    }
    data.update(overrides)
    return Masters(**data)


def make_request(**overrides):
    req = {
        "requestId": "REQ-20261124093000-123456",
        "applicantEmail": "applicant@lineworks-local.info",
        "applicantName": "申請 太郎",
        "department": "製造部",
        "requestDate": "2026-11-24",
        "reasonCode": "A",
        "reasonDetail": "ウエスが切れたため",
        "totalAmount": 0,
        "amountWaived": "",
        "category": "メカ",
        "status": "承認中",
        "currentStep": "上席",
        "currentApproverEmail": "sup1@lineworks-local.info",
        "currentApproverName": "上田 一郎",
        "routeJson": '["上席","購買見積","総務部長","購買手配"]',
        "pdfFileId": "",
        "pdfUrl": "",
    }
    req.update(overrides)
    return req


def make_items():
    return [
        {"itemId": "ITEM-2", "requestId": "REQ-20261124093000-123456", "lineNo": 2, "name": "オイル", "model": "",
         "maker": "", "quantity": 1.5, "unit": "L", "unitPrice": 0, "amount": 0, "desiredDeliveryDate": "", "note": ""},
        {"itemId": "ITEM-1", "requestId": "REQ-20261124093000-123456", "lineNo": 1, "name": "ウエス", "model": "W-100",
         "maker": "ABC", "quantity": 3, "unit": "ケース", "unitPrice": 1200, "amount": 3600,
         "desiredDeliveryDate": "2026-12-01", "note": "至急"},
    ]


@pytest.fixture
def masters():
    return make_masters()


@pytest.fixture
def request_row():
    return make_request()
