"""業務ロジック（担当 W）の受け入れテスト。期待値は GAS 版（Workflow.gs ほか）の動きから作る。

呼び出しは rpc.dispatch を通す（業務エラーでのロールバック・JSON の形・二重押し対策まで確かめるため）。
"""
from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from app import rpc
from app.auth import user_from_email
from app.constants import STATUS, STEPS
from app.models import (
    ApproverRule,
    DeptSupervisor,
    History,
    Job,
    NotificationRecipient,
    Request,
    RequestItem,
    Setting,
    SyncRun,
    Worker,
)
from app.timeutil import today_jst
from tests.conftest import make_settings

D = "@lineworks-local.info"
ADMIN = "imaizumi" + D
APP1 = "app1" + D        # 製造部の申請者
APP2 = "app2" + D        # 設計部の申請者
SUP1 = "sup1" + D        # 製造部の上席（代表）
SUP2 = "sup2" + D        # 製造部の上席
SUP3 = "sup3" + D        # 設計部の上席
GM = "gm" + D
GM2 = "gm2" + D
PRES = "pres" + D
MECH = "mech" + D
ELEC = "elec" + D
OTHER = "other" + D

ERR_REOPEN = "画面を開いた後に申請の段が変わりました。画面を開き直してください。"


@pytest.fixture(autouse=True)
def handlers():
    rpc.reset_registry()
    assert rpc.load_domain_handlers()
    yield
    rpc.reset_registry()


@pytest.fixture
def seeded(db):
    db.add(ApproverRule(
        sort_order=1, department="*", general_manager_email=f"{GM}, {GM2}", general_manager_name="辻野（総務部長）",
        general_manager_title="部長", president_email=PRES, president_name="社長 太郎", president_title="代表取締役",
        purchasing_email=MECH, purchasing_name="五十嵐（購買）", purchasing_title="係長",
        purchasing_elec_email=ELEC, purchasing_elec_name="電気 一郎", active=True,
    ))
    db.add(ApproverRule(sort_order=2, department="製造部", active=True))
    db.add(ApproverRule(sort_order=3, department="設計部", active=True))
    db.add(DeptSupervisor(sort_order=1, department="製造部", email=SUP1, name="上田 一", title="課長", active=True))
    db.add(DeptSupervisor(sort_order=2, department="製造部", email=SUP2, name="上野 二", active=True,
                          notify_email="sup2-notify" + D))
    db.add(DeptSupervisor(sort_order=3, department="設計部", email=SUP3, name="設計 三", active=True))
    db.add(Setting(key="thresholdAmount", value="100000"))
    db.add(Setting(key="adminEmails", value=ADMIN))
    db.add(Setting(key="enableEmailNotifications", value="true"))
    db.add(NotificationRecipient(sort_order=1, type="総務部", email="ga" + D, name="総務", active=True, send_as="TO"))
    db.commit()
    return db


class Caller:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings

    def raw(self, email, fn, *args, key=None):
        response = rpc.dispatch(fn, list(args), db=self.db, user=user_from_email(email), settings=self.settings,
                                idempotency_key=key)
        return response.status_code, json.loads(response.body)

    def ok(self, email, fn, *args, key=None):
        status, body = self.raw(email, fn, *args, key=key)
        assert status == 200, body
        return body["result"]

    def err(self, email, fn, *args):
        status, body = self.raw(email, fn, *args)
        assert status == 400, body
        return body["error"]


@pytest.fixture
def call(seeded, settings):
    return Caller(seeded, settings)


def payload(**over):
    data = {
        "requestDate": "2026-11-20",
        "applicantName": "申請 花子",
        "department": "製造部",
        "category": "メカ",
        "reasonCode": "A",
        "reasonDetail": "消耗品の補充",
        "items": [
            {"name": "ウエス", "model": "W-1", "maker": "A社", "quantity": 3, "unit": "ケース",
             "desiredDeliveryDate": "2026-12-01", "note": ""},
        ],
    }
    data.update(over)
    return data


def items(n):
    return [{"name": f"品{i}", "model": f"M{i}", "maker": "", "quantity": i, "unit": "個",
             "desiredDeliveryDate": "", "note": ""} for i in range(1, n + 1)]


def create(call, email=APP1, **over):
    return call.ok(email, "createRequest", payload(**over))["request"]["requestId"]


def jobs(db, request_id=None, kind=None):
    stmt = select(Job).order_by(Job.id)
    rows = list(db.execute(stmt).scalars())
    return [j for j in rows if (request_id is None or j.request_id == request_id) and (kind is None or j.kind == kind)]


def args(job):
    return json.loads(job.args_json)


def req(db, request_id) -> Request:
    db.expire_all()
    return db.get(Request, request_id)


def detail(call, request_id, email=ADMIN):
    return call.ok(email, "getRequestDetail", request_id)


def to_quote(call, rid):
    call.ok(SUP1, "approveRequest", rid, "", False)


def to_gm(call, rid, price=1000):
    to_quote(call, rid)
    d = detail(call, rid, MECH)
    call.ok(MECH, "confirmQuote", rid, [{"itemId": it["itemId"], "unitPrice": price} for it in d["items"]], "")


# ===== 申請 =====


def test_create_request_shape_route_history_and_jobs(call, db):
    d = call.ok(APP1, "createRequest", payload())
    r = d["request"]
    assert r["requestId"].startswith("REQ-") and len(r["requestId"]) == len("REQ-20261120123456-123456")
    assert (r["status"], r["currentStep"], r["currentApproverEmail"], r["currentApproverName"]) == (
        "承認中", "上席", SUP1, "上田 一")
    assert r["route"] == ["上席", "購買見積", "総務部長", "購買手配"]
    assert r["applicantEmail"] == APP1 and r["applicantName"] == "申請 花子"
    assert r["totalAmount"] == 0 and r["amountWaived"] is False and r["categoryLabel"] == "メカ"
    assert r["reasonLabel"].startswith("4月以降") and r["statusLabel"] == "承認中" and r["currentStepLabel"] == "上席"
    assert r["requestDate"] == "2026-11-20" and len(r["createdAt"]) == 19 and "T" not in r["createdAt"]
    assert d["items"][0]["unit"] == "ケース" and d["items"][0]["desiredDeliveryDate"] == "2026-12-01"
    assert d["items"][0]["amount"] == 0 and d["items"][0]["lineNo"] == 1
    h = d["history"][0]
    assert (h["action"], h["actorEmail"], h["actorName"], h["toStatus"], h["toStep"], h["fromStatus"]) == (
        "申請", APP1, "申請 花子", "承認中", "上席", "")
    assert h["actionLabel"] == "申請"
    # 上席は全員へ承認依頼（GAS: supervisors.forEach(defer_('sendApprovalRequestEmail_', [request, s]))）
    js = jobs(db, r["requestId"])
    assert [j.kind for j in js] == ["sendApprovalRequestEmail_"] * 2
    assert [args(j)[1] for j in js] == [
        {"email": SUP1, "name": "上田 一", "title": "課長", "notifyEmail": ""},
        {"email": SUP2, "name": "上野 二", "title": "", "notifyEmail": "sup2-notify" + D},
    ]
    assert args(js[0])[0]["requestId"] == r["requestId"] and args(js[0])[0]["status"] == "承認中"
    assert all((j.guard_status, j.guard_step, j.state) == ("承認中", "上席", "pending") for j in js)
    assert d["pendingJobs"] == 2 and "deferredJobs" not in d


def test_request_date_defaults_to_jst_today(call):
    d = call.ok(APP1, "createRequest", payload(requestDate=""))
    assert d["request"]["requestDate"] == today_jst().strftime("%Y-%m-%d")


@pytest.mark.parametrize("over, message", [
    ({"department": " "}, "部署を入力してください。"),
    ({"category": ""}, "品目区分（メカ／電気／一般・不明）を選択してください。"),
    ({"category": "MECH"}, "品目区分（メカ／電気／一般・不明）を選択してください。"),
    ({"reasonCode": "Z"}, "購入理由A-Fを選択してください。"),
    ({"items": []}, "明細を1行以上入力してください。"),
    ({"items": [{"name": "", "model": "", "maker": "", "quantity": "", "note": ""}]}, "明細を1行以上入力してください。"),
    ({"items": [{"name": "", "model": "X", "quantity": 1}]}, "明細1行目の品名を入力してください。"),
    ({"items": [{"name": "A", "quantity": 1}, {"name": "B", "quantity": -1}]}, "明細2行目の数量を1以上にしてください。"),
    ({"items": [{"name": "A", "quantity": 1, "unitPrice": -5}]}, "明細1行目の単価は0以上で入力してください。"),
    ({"items": [{"name": "A", "quantity": 1, "desiredDeliveryDate": "来週"}]},
     "明細1行目の希望納期は日付（yyyy-MM-dd）で入力してください。"),
    ({"requestDate": "きょう"}, "申請日は日付（yyyy-MM-dd）で入力してください。"),
])
def test_create_request_validation_messages(call, db, over, message):
    assert call.err(APP1, "createRequest", payload(**over)) == message
    assert db.execute(select(Request)).first() is None  # 何も書かない


def test_input_is_trimmed_and_cut_silently_and_decimals_allowed(call):
    long = "あ" * 2100
    d = call.ok(APP1, "createRequest", payload(
        reasonCode="AB", reasonDetail=long, department="製造部" + " ",
        items=[{"name": "  3/8 配管 " + "x" * 300, "model": "0123", "maker": "=1+1", "quantity": "1.5",
                "unit": "__other__", "note": "1/2"}]))
    r = d["request"]
    assert r["reasonCode"] == "A"  # sanitizeText_(…, 1)
    assert len(r["reasonDetail"]) == 2000 and r["department"] == "製造部"
    it = d["items"][0]
    assert it["name"].startswith("3/8 配管") and len(it["name"]) == 200
    assert (it["model"], it["maker"], it["note"], it["unit"], it["quantity"]) == ("0123", "=1+1", "1/2", "", 1.5)


def test_create_without_approver_rule_or_supervisor(call, db):
    db.query(ApproverRule).delete()
    db.commit()
    assert call.err(APP1, "createRequest", payload()) == \
        "承認者マスタが未設定です。管理画面で部署または申請者の承認者を登録してください。"
    db.add(ApproverRule(sort_order=1, department="*", general_manager_email=GM, president_email=PRES,
                        purchasing_email=MECH, active=True))
    db.commit()
    assert call.err(APP1, "createRequest", payload(department="無い部署")) == \
        "上席承認者が未設定です。部署別 上席マスタ（または承認者マスタの上席）を設定してください。"


def test_missing_purchasing_is_rejected_at_submit(call, db):
    db.query(ApproverRule).delete()
    db.add(ApproverRule(sort_order=1, department="*", general_manager_email=GM, active=True))
    db.commit()
    assert call.err(APP1, "createRequest", payload()) == "購買（見積）の承認者メールアドレスが未設定です。"


def test_approver_rule_lookup_order_and_star_fill(call, db):
    # 申請者メールの行が部署の行より先に当たる。役職が空なら * で補う。
    db.add(ApproverRule(sort_order=0, department="", applicant_email=APP1, general_manager_email=OTHER, active=True))
    db.add(ApproverRule(sort_order=0, department="製造部", general_manager_email="inactive" + D, active=False))
    db.commit()
    rid = create(call)
    to_gm(call, rid)
    assert req(db, rid).current_approver_email == OTHER  # 申請者別の行
    rid2 = create(call, email=APP2, department="設計部")
    to_quote_by = SUP3
    call.ok(to_quote_by, "approveRequest", rid2, "", False)
    assert req(db, rid2).current_approver_email == MECH  # 部署の行に購買が無い → *


# ===== 承認・権限 =====


def test_approve_by_supervisor_moves_to_quote_with_category_representative(call, db):
    rid = create(call, category="電気")
    assert call.err(OTHER, "approveRequest", rid, "", False) == "現在の承認者のみ承認できます。"
    d = call.ok(SUP2, "approveRequest", rid, "確認しました", False)  # 代表でなくても上席集合なら承認できる
    r = d["request"]
    assert (r["currentStep"], r["currentApproverEmail"], r["currentApproverName"]) == ("購買見積", ELEC, "電気 一郎")
    assert d["permissions"]["canQuote"] is False  # SUP2 から見た権限ではない（ADMIN で見ていない）
    h = d["history"][-1]
    assert (h["action"], h["actorEmail"], h["actorName"], h["fromStep"], h["toStep"], h["comment"]) == (
        "承認", SUP2, "sup2", "上席", "購買見積", "確認しました")
    job = jobs(db, rid, "dispatchApprovalNotifications_")[0]
    a = args(job)
    assert a[1:] == ["上席", "購買見積", {"email": ELEC, "name": "電気 一郎", "title": ""}, {"email": SUP2, "name": "sup2"}]
    assert a[0]["currentStep"] == "購買見積" and (job.guard_status, job.guard_step) == ("承認中", "購買見積")
    # メカ購買も操作できる（品目区分によらず両方の購買）
    assert detail(call, rid, MECH)["permissions"]["canQuote"] is True
    assert call.err(SUP1, "approveRequest", rid, "", False) == "現在の承認者のみ承認できます。"
    assert call.err(MECH, "approveRequest", rid, "", False) == "購買（見積）ステップでは見積金額を入力してください。"


def test_supervisor_of_other_department_cannot_approve(call):
    rid = create(call)
    assert call.err(SUP3, "approveRequest", rid, "", False) == "現在の承認者のみ承認できます。"
    assert call.ok(ADMIN, "approveRequest", rid, "", False)["request"]["currentStep"] == "購買見積"


def test_dept_supervisor_fallbacks(call, db):
    db.add(DeptSupervisor(sort_order=9, department="*", email="star-sup" + D, name="", active=True))
    db.commit()
    rid = create(call, department="総務課")  # 部署一致なし → '*'
    assert req(db, rid).current_approver_email == "star-sup" + D
    assert req(db, rid).current_approver_name == "上席"


# ===== 見積・税・社長決裁 =====


@pytest.mark.parametrize("price, over", [(99999, False), (100000, True)])
def test_confirm_quote_threshold_boundary(call, db, price, over):
    rid = create(call)
    to_quote(call, rid)
    d = detail(call, rid, MECH)
    res = call.ok(MECH, "confirmQuote", rid, [{"itemId": d["items"][0]["itemId"], "unitPrice": price / 3}], "急ぎ")
    r = res["request"]
    assert r["totalAmount"] == price and r["currentStep"] == "総務部長" and r["currentApproverEmail"] == GM
    assert r["route"] == (["上席", "購買見積", "総務部長", "社長", "購買手配"] if over
                          else ["上席", "購買見積", "総務部長", "購買手配"])
    assert res["items"][0]["unit"] == "ケース"  # 単位が消えない
    tax = price // 10
    summary = f"税抜 ¥{price:,}（消費税 ¥{tax:,}／税込 ¥{price + tax:,}）"
    assert res["history"][-1]["comment"] == "見積金額 " + summary + ("（税抜10万円以上）" if over else "") + " 急ぎ"
    assert res["history"][-1]["action"] == "金額確定"
    ga, mail = jobs(db, rid, "notifyGeneralAffairs_")[0], jobs(db, rid, "sendApprovalRequestEmail_")[-1]
    subject = args(ga)[1]
    if over:
        assert subject == f"【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 {rid}（税抜10万円以上）"
        assert "要・社長決裁" in args(mail)[2] and "要・社長決裁" in args(ga)[2]
    else:
        assert subject == f"[貯蔵品購入申請] 金額確定 {rid}"
        assert args(mail)[2] == ""
    assert "総務部長承認をお願いします" in args(ga)[2] or over
    assert args(ga)[3] is None and args(mail)[1]["email"] == GM  # 総務部長は代表 1 人だけ（決定事項）
    assert (ga.guard_status, ga.guard_step) == ("承認中", "総務部長")


def test_confirm_quote_amount_rounding_and_tax_floor(call, db):
    rid = create(call, items=[{"name": "A", "quantity": 3, "unit": "個"}, {"name": "B", "quantity": 0.5, "unit": "個"}])
    to_quote(call, rid)
    d = detail(call, rid, MECH)
    res = call.ok(MECH, "confirmQuote", rid, [
        {"itemId": d["items"][0]["itemId"], "unitPrice": "33.5"},  # 100.5 → 101（四捨五入）
        {"itemId": d["items"][1]["itemId"], "unitPrice": "1,001"},  # 500.5 → 501
    ], "")
    assert [i["amount"] for i in res["items"]] == [101, 501]
    assert res["request"]["totalAmount"] == 602
    assert "消費税 ¥60／税込 ¥662" in res["history"][-1]["comment"]  # 60.2 → 60（切り捨て、合計に 1 回）


def test_confirm_quote_errors(call, db):
    rid = create(call)
    assert call.err(MECH, "confirmQuote", rid, [], "") == "購買担当者のみ見積金額を入力できます。"
    to_quote(call, rid)
    d = detail(call, rid, MECH)
    item_id = d["items"][0]["itemId"]
    assert call.err(OTHER, "confirmQuote", rid, [{"itemId": item_id, "unitPrice": 1}], "") == \
        "購買担当者のみ見積金額を入力できます。"
    assert call.err(MECH, "confirmQuote", rid, [{"itemId": item_id, "unitPrice": 0}], "") == "金額を入力してください。"
    assert call.err(MECH, "confirmQuote", rid, [{"itemId": item_id, "unitPrice": -1}], "") == \
        "明細1行目の単価は0以上で入力してください。"
    assert req(db, rid).current_step == "購買見積"


def test_full_flow_with_president_and_completion(call, db):
    rid = create(call)
    to_gm(call, rid, price=50000)  # 3 個 × 5 万 = 15 万 → 社長決裁
    assert call.err(PRES, "approveRequest", rid, "", False) == "現在の承認者のみ承認できます。"
    d = call.ok(GM2, "approveRequest", rid, "", False)  # 総務部長は集合の誰でも
    assert d["request"]["currentStep"] == "社長" and d["request"]["isPresidentPending"] is True
    a = args(jobs(db, rid, "dispatchApprovalNotifications_")[-1])
    assert a[2] == "社長" and a[3] == {"email": PRES, "name": "社長 太郎", "title": "代表取締役"}
    assert call.err(GM, "recordPresidentDecision", rid, "approve", "", False) == \
        "社長または管理者（総務部長）のみ社長決裁を記録できます。"
    d = call.ok(PRES, "recordPresidentDecision", rid, "approve", "", False)
    assert d["request"]["currentStep"] == "購買手配" and d["request"]["currentApproverEmail"] == MECH
    assert d["history"][-1]["actorName"] == "pres" and d["history"][-1]["fromStep"] == "社長"
    assert call.err(MECH, "approveRequest", rid, "", False) == "購買ステップでは手配完了を実行してください。"
    assert call.err(OTHER, "arrangeComplete", rid, "") == "購買担当者のみ手配完了できます。"
    d = call.ok(ELEC, "arrangeComplete", rid, "発注済み")
    r = d["request"]
    assert (r["status"], r["currentStep"], r["currentApproverEmail"]) == ("完了", "完了済", "")
    assert r["completedAt"] and d["history"][-1]["action"] == "完了処理"
    job = jobs(db, rid, "completeAndNotify_")[0]
    assert args(job)[1] == {"email": ELEC, "name": "elec"} and (job.guard_status, job.guard_step) == ("完了", "完了済")


def test_president_decision_by_admin_is_proxy_and_return(call, db):
    rid = create(call)
    to_gm(call, rid, price=50000)
    call.ok(GM, "approveRequest", rid, "", False)
    assert call.err(ADMIN, "recordPresidentDecision", rid, "return", "", False) == "差戻し理由を入力してください。"
    assert call.err(ADMIN, "recordPresidentDecision", rid, "hold", "", False) == "不正な操作です。"
    d = call.ok(ADMIN, "recordPresidentDecision", rid, "return", "高すぎる", False)
    h = d["history"][-1]
    assert (h["actorEmail"], h["actorName"], h["comment"]) == (PRES, "社長 太郎", "高すぎる （imaizumiが社長決裁モードで記録）")
    assert d["request"]["status"] == "差戻し"
    b = jobs(db, rid, "sendReturnedBroadcast_")[0]
    assert args(b)[1:] == ["社長", "高すぎる", "社長 太郎"] and b.guard_status == "" and b.guard_step == ""
    assert args(b)[0]["currentStep"] == "社長"  # 差戻し前の申請の写し


def test_arrange_complete_requires_amount_unless_waived(call, db):
    rid = create(call)
    to_gm(call, rid)
    call.ok(GM, "approveRequest", rid, "", False)
    r = req(db, rid)
    r.total_amount = 0
    db.commit()
    assert call.err(MECH, "arrangeComplete", rid, "") == \
        "確定金額が未入力です。見積（金額入力）が未完了の案件は、差戻し→再申請してください。"


def test_expedited_flow_and_resubmit_clears_waived(call, db):
    rid = create(call)
    to_quote(call, rid)
    assert call.err(OTHER, "confirmExpedited", rid, "") == "購買担当者のみ操作できます。"
    d = call.ok(MECH, "confirmExpedited", rid, "CO2")
    r = d["request"]
    assert r["amountWaived"] is True and r["currentStep"] == "総務部長" and r["totalAmount"] == 0
    assert d["history"][-1]["comment"] == "金額確定不要・至急手配を依頼 CO2"
    assert d["permissions"]["canEditInReview"] is False
    ga = jobs(db, rid, "notifyGeneralAffairs_")[0]
    assert args(ga)[1] == "【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] " + rid
    assert "備考: CO2" in args(ga)[2]
    mail = jobs(db, rid, "sendApprovalRequestEmail_")[-1]
    assert args(mail)[3] == "【⚡至急承認・金額確定不要⚡】" and args(mail)[1]["email"] == GM
    call.ok(GM, "approveRequest", rid, "", False)
    assert call.ok(MECH, "arrangeComplete", rid, "")["request"]["status"] == "完了"  # 金額なしで完了できる
    # 再申請では金額不要を戻す（決定事項）
    rid2 = create(call)
    to_quote(call, rid2)
    call.ok(MECH, "confirmExpedited", rid2, "")
    call.ok(GM, "returnRequest", rid2, "やり直し", False, False)
    d = call.ok(OTHER, "resubmitRequest", rid2)
    assert d["request"]["amountWaived"] is False and d["request"]["currentStep"] == "上席"


# ===== 全画面（kiosk）と expectedStep =====


def test_kiosk_checks_state_and_step(call, db):
    rid = create(call)
    # 代理記録: 誰でも記録できるが、印と履歴は今の段の正規の承認者
    d = call.ok(OTHER, "approveRequest", rid, "よし", True, None, "上席")
    h = d["history"][-1]
    assert (h["actorEmail"], h["actorName"], h["comment"]) == (SUP1, "上田 一", "よし （otherの端末で代理記録）")
    # 購買見積の段は全画面で操作できない
    assert call.err(OTHER, "approveRequest", rid, "", True, None, "購買見積") == \
        "全画面モードで操作できる申請ではありません。画面を開き直してください。"
    assert call.err(OTHER, "returnRequest", rid, "x", True, False, "購買見積") == \
        "全画面モードで操作できる申請ではありません。画面を開き直してください。"
    # 画面を開いた段と違う（2 台で同時に押した）
    assert call.err(OTHER, "approveRequest", rid, "", True, None, "上席") == ERR_REOPEN
    assert call.err(MECH, "approveRequest", rid, "", False, None, "上席") == ERR_REOPEN
    # 取消済みは復活できない
    rid2 = create(call)
    call.ok(APP1, "deleteRequest", rid2, "")
    assert call.err(OTHER, "approveRequest", rid2, "", True, None, "上席") == \
        "全画面モードで操作できる申請ではありません。画面を開き直してください。"
    assert req(db, rid2).status == "取消"


def test_kiosk_requires_expected_step(call, db):
    rid = create(call)
    for missing in (None, ""):
        assert call.err(OTHER, "approveRequest", rid, "", True, None, missing) == ERR_REOPEN
        assert call.err(OTHER, "returnRequest", rid, "x", True, False, missing) == ERR_REOPEN
    assert call.err(OTHER, "approveRequest", rid, "", True) == ERR_REOPEN
    assert req(db, rid).current_step == "上席"
    # 全画面でなければ省略できる（GAS 版と同じ呼び方）
    call.ok(SUP1, "approveRequest", rid, "", False)


def test_detail_write_operations_check_expected_step(call, db):
    rid = create(call)
    call.ok(SUP1, "approveRequest", rid, "", False, None, "上席")
    # 購買見積の段: 見積確定・至急・リコールは画面の段と違えば止める
    item_id = detail(call, rid, MECH)["items"][0]["itemId"]
    assert call.err(MECH, "confirmQuote", rid, [{"itemId": item_id, "unitPrice": 100}], "", None, "上席") == ERR_REOPEN
    assert call.err(MECH, "confirmExpedited", rid, "", "上席") == ERR_REOPEN
    assert call.err(MECH, "recallStep", rid, "戻す", "上席") == ERR_REOPEN
    assert req(db, rid).current_step == "購買見積"
    call.ok(MECH, "confirmQuote", rid, [{"itemId": item_id, "unitPrice": 100}], "", None, "購買見積")
    call.ok(GM, "approveRequest", rid, "", False, None, "総務部長")
    assert call.err(MECH, "arrangeComplete", rid, "", "総務部長") == ERR_REOPEN
    call.ok(MECH, "recallStep", rid, "戻す", "購買手配")
    assert req(db, rid).current_step == "総務部長"


def test_expected_step_counts_as_an_argument(call, db):
    rid = create(call)
    assert call.err(SUP1, "recallStep", rid, "c", "上席", "余分") == "引数の数が正しくありません。"
    assert call.err(SUP1, "arrangeComplete", rid, "c", "上席", "余分") == "引数の数が正しくありません。"
    assert call.err(SUP1, "confirmExpedited", rid, "c", "上席", "余分") == "引数の数が正しくありません。"
    assert call.err(SUP1, "confirmQuote", rid, [], "c", None, "上席", "余分") == "引数の数が正しくありません。"


def test_item_count_limit(call, db):
    many = [{"name": f"品{i}", "quantity": 1, "unit": "個"} for i in range(101)]
    assert call.err(APP1, "createRequest", payload(items=many)) == "明細は100行までにしてください。"
    rid = create(call, items=many[:100])
    assert len(detail(call, rid)["items"]) == 100


def test_kiosk_return_and_reject(call, db):
    rid = create(call)
    assert call.err(OTHER, "returnRequest", rid, "x", True, True, "上席") == "全画面モードでは却下できません。"
    assert call.err(OTHER, "returnRequest", rid, "", True, False, "上席") == "差戻しコメントを入力してください。"
    d = call.ok(OTHER, "returnRequest", rid, "数量違い", True, False, "上席")
    assert d["request"]["status"] == "差戻し"
    assert d["history"][-1]["comment"] == "数量違い （otherの端末で代理記録）"
    assert args(jobs(db, rid, "sendReturnedBroadcast_")[0])[3] == "上田 一"


def test_kiosk_president_requires_president_pending(call, db):
    rid = create(call)
    to_gm(call, rid, price=50000)
    assert call.err(OTHER, "recordPresidentDecision", rid, "approve", "", True) == ERR_REOPEN  # 全画面は段が必須
    assert call.err(OTHER, "recordPresidentDecision", rid, "approve", "", True, None, "総務部長") == \
        "社長決裁待ちの申請ではありません。画面を開き直してください。"
    # expectedStep は状態の確認より先に照合する
    assert call.err(OTHER, "recordPresidentDecision", rid, "approve", "", True, None, "社長") == ERR_REOPEN
    call.ok(GM, "approveRequest", rid, "", False)
    assert call.err(OTHER, "recordPresidentDecision", rid, "approve", "", True, None, "総務部長") == ERR_REOPEN
    d = call.ok(OTHER, "recordPresidentDecision", rid, "approve", "", True, None, "社長")
    assert d["history"][-1]["comment"] == "（otherが社長決裁モードで記録）"
    assert d["request"]["currentStep"] == "購買手配"


# ===== 差戻し・却下・差戻しプール・取消 =====


def test_return_moves_to_pool_and_jobs(call, db):
    rid = create(call)
    to_quote(call, rid)
    assert call.err(OTHER, "returnRequest", rid, "x", False, False) == "現在の承認者のみ差戻し・却下できます。"
    assert call.err(MECH, "returnRequest", rid, " ", False, False) == "差戻しコメントを入力してください。"
    d = call.ok(MECH, "returnRequest", rid, "型式不明", False, False)
    r = d["request"]
    assert (r["status"], r["currentStep"], r["currentApproverEmail"]) == ("差戻し", "申請者", "")
    assert (d["history"][-1]["fromStep"], d["history"][-1]["toStep"]) == ("購買見積", "申請者")
    ret = jobs(db, rid, "sendReturnedEmail_")[0]
    assert args(ret)[1] == "型式不明" and (ret.guard_status, ret.guard_step) == ("差戻し", "申請者")
    b = jobs(db, rid, "sendReturnedBroadcast_")[0]
    assert args(b)[1:] == ["購買見積", "型式不明", "mech"] and args(b)[0]["status"] == "承認中"
    # 差戻しプール: 誰でも編集・再申請・削除できる
    p = detail(call, rid, OTHER)["permissions"]
    assert p["canEdit"] and p["canResubmit"] and p["canDeletePool"] and p["canDelete"]


def test_reject_cancels_keeps_step_and_trashes_pdf(call, db):
    rid = create(call)
    r = req(db, rid)
    r.pdf_file_id, r.pdf_url = "file-1", "https://drive/file-1"
    db.commit()
    assert call.err(SUP1, "returnRequest", rid, "", False, True) == "却下理由（コメント）を入力してください。"
    d = call.ok(SUP1, "returnRequest", rid, "不要", False, True)
    r = d["request"]
    assert (r["status"], r["currentStep"], r["pdfFileId"], r["pdfUrl"]) == ("取消", "上席", "", "")
    assert d["history"][-1]["action"] == "却下" and d["history"][-1]["toStep"] == "上席"
    trash = jobs(db, rid, "trashPdf")[0]
    assert args(trash) == ["file-1", rid] and trash.guard_status == ""
    rej = jobs(db, rid, "sendRejectedEmail_")[0]
    assert args(rej)[1] == "不要" and (rej.guard_status, rej.guard_step) == ("取消", "上席")


def test_pool_edit_resubmit(call, db):
    rid = create(call)
    to_gm(call, rid)
    call.ok(GM, "returnRequest", rid, "戻す", False, False)
    assert call.err(OTHER, "resubmitRequest", create(call)) == "差戻し中の申請のみ再申請できます。"
    d = call.ok(OTHER, "updateRequestDraft", rid, payload(items=items(2)))
    assert d["request"]["status"] == "差戻し" and len(d["items"]) == 2 and d["request"]["totalAmount"] == 0
    assert d["history"][-1]["comment"] == "申請内容を修正" and d["history"][-1]["actorEmail"] == OTHER
    r = req(db, rid)
    r.pdf_file_id = "old-pdf"
    db.commit()
    d = call.ok(OTHER, "resubmitRequest", rid)
    r = d["request"]
    assert (r["status"], r["currentStep"], r["currentApproverEmail"], r["totalAmount"], r["pdfFileId"]) == (
        "承認中", "上席", SUP1, 0, "")
    assert d["history"][-1]["action"] == "再申請" and d["history"][-1]["fromStep"] == "申請者"
    assert len(jobs(db, rid, "sendApprovalRequestEmail_")) == 5  # 申請時 2 ＋ 総務部長へ 1 ＋ 再申請 2
    assert jobs(db, rid, "trashPdf") == []  # ファイルは消さない
    assert call.err(OTHER, "updateRequestDraft", rid, payload()) == "差戻し中の申請のみ修正できます。"


def test_delete_permissions(call, db):
    rid = create(call)
    assert call.err(OTHER, "deleteRequest", rid, "") == "この申請を取り消す権限がありません。"
    d = call.ok(APP1, "deleteRequest", rid, "不要になった")
    assert d["request"]["status"] == "取消" and d["history"][-1]["action"] == "取消処理"
    assert call.err(APP1, "deleteRequest", rid, "") == "この申請を取り消す権限がありません。"
    # 完了後は申請者は取り消せない。管理者は取り消せ、PDF をゴミ箱へ
    rid2 = create(call)
    to_gm(call, rid2)
    call.ok(GM, "approveRequest", rid2, "", False)
    call.ok(MECH, "arrangeComplete", rid2, "")
    r = req(db, rid2)
    r.pdf_file_id = "done-pdf"
    db.commit()
    assert call.err(APP1, "deleteRequest", rid2, "") == "この申請を取り消す権限がありません。"
    call.ok(ADMIN, "deleteRequest", rid2, "")
    assert args(jobs(db, rid2, "trashPdf")[0]) == ["done-pdf", rid2]


# ===== リコール =====


def test_recall_permissions_and_effects(call, db):
    rid = create(call)
    assert call.err(SUP1, "recallStep", rid, "x") == "この申請を前段へ戻す権限がありません。"  # 先頭
    to_quote(call, rid)
    assert call.err(OTHER, "recallStep", rid, "x") == "この申請を前段へ戻す権限がありません。"
    assert call.err(SUP1, "recallStep", rid, "") == "戻す理由（コメント）を入力してください。"
    d = call.ok(SUP1, "recallStep", rid, "誤って承認")  # 進めた本人
    assert d["request"]["currentStep"] == "上席" and d["request"]["currentApproverEmail"] == SUP1
    assert d["history"][-1]["action"] == "前段戻し"
    recall_jobs = jobs(db, rid, "sendApprovalRequestEmail_")[-2:]
    assert [args(j)[1]["email"] for j in recall_jobs] == [SUP1, SUP2]


def test_recall_to_quote_resets_waived_and_route_but_keeps_total(call, db):
    rid = create(call)
    to_gm(call, rid, price=50000)
    assert req(db, rid).route_json.count("社長") == 1
    d = call.ok(MECH, "recallStep", rid, "金額を直す")  # 金額確定した本人
    r = d["request"]
    assert r["currentStep"] == "購買見積" and r["route"] == ["上席", "購買見積", "総務部長", "購買手配"]
    assert r["totalAmount"] == 150000 and r["amountWaived"] is False and r["currentApproverEmail"] == MECH
    assert [args(j)[1]["email"] for j in jobs(db, rid, "sendApprovalRequestEmail_")[-2:]] == [MECH, ELEC]
    # 明細を直せて、合計は 0 に戻る（unit-design 4-7）
    assert d["permissions"]["canEditInReview"] is True
    d = call.ok(APP1, "updateRequestInReview", rid, {"items": [dict(payload()["items"][0], unit="個")]})
    assert d["request"]["totalAmount"] == 0
    assert d["history"][-1]["comment"] == "明細1 単位: ケース→個"


def test_recall_from_expedited_gm(call, db):
    rid = create(call)
    to_quote(call, rid)
    call.ok(MECH, "confirmExpedited", rid, "")
    d = call.ok(MECH, "recallStep", rid, "至急は誤り")
    assert d["request"]["amountWaived"] is False and d["request"]["currentStep"] == "購買見積"


# ===== 承認前の明細修正 =====


def test_update_in_review_summary_and_window(call, db):
    rid = create(call, items=items(2))
    new = items(2)
    new[0]["model"] = "X1"
    new[1]["quantity"] = 5
    new.append({"name": "追加品", "quantity": 1})
    d = call.ok(OTHER, "updateRequestInReview", rid, {"items": new})
    assert d["history"][-1]["comment"] == "明細1 型式: M1→X1 ／ 明細2 数量: 2→5 ／ 明細3: 追加（追加品）"
    assert d["history"][-1]["actorEmail"] == OTHER
    job = jobs(db, rid, "sendRequestUpdatedEmail_")[0]
    assert args(job)[1:] == [d["history"][-1]["comment"], "other", OTHER]
    # 変更なしなら何も書かない
    count = len(d["history"])
    same = [{k: v for k, v in it.items() if k != "itemId"} for it in d["items"]]
    d2 = call.ok(OTHER, "updateRequestInReview", rid, {"items": same})
    assert len(d2["history"]) == count and len(jobs(db, rid, "sendRequestUpdatedEmail_")) == 1
    # 明細 ID は位置で引き継ぐ（変わらない ID）
    assert [i["itemId"] for i in d2["items"]] == [i["itemId"] for i in d["items"]]
    # 送られてきた単価は 0 にする
    d3 = call.ok(OTHER, "updateRequestInReview", rid, {"items": [dict(same[0], unitPrice=999)]})
    assert d3["items"][0]["unitPrice"] == 0
    assert d3["history"][-1]["comment"] == "明細2: 削除（品2） ／ 明細3: 削除（追加品）"
    to_gm(call, rid)
    assert call.err(OTHER, "updateRequestInReview", rid, {"items": same}) == \
        "この申請は現在修正できません。承認中かつ購買が金額を入力する前の申請のみ、明細を修正できます。"


# ===== 分割（split-design 7 章） =====


def refs(d, *indexes):
    return [{"itemId": d["items"][i]["itemId"], "lineNo": d["items"][i]["lineNo"], "name": d["items"][i]["name"]}
            for i in indexes]


def test_split_1_supervisor_approve_with_one_item_out(call, db):
    rid = create(call, items=items(4))
    d = detail(call, rid, SUP1)
    assert d["permissions"]["canSplitItems"] is True
    res = call.ok(SUP1, "approveRequest", rid, "品2は不要", False, refs(d, 1))
    assert [i["name"] for i in res["items"]] == ["品1", "品3", "品4"] and [i["lineNo"] for i in res["items"]] == [1, 2, 3]
    assert res["request"]["currentStep"] == "購買見積"
    split = db.execute(select(Request).where(Request.split_from_request_id == rid)).scalar_one()
    sd = detail(call, split.request_id, APP1)
    r = sd["request"]
    assert (r["status"], r["currentStep"], r["totalAmount"], r["splitFromRequestId"], r["applicantEmail"]) == (
        "差戻し", "申請者", 0, rid, APP1)
    assert r["route"] == ["上席", "購買見積", "総務部長", "購買手配"] and r["requestDate"] == "2026-11-20"
    assert [i["name"] for i in sd["items"]] == ["品2"] and sd["items"][0]["itemId"] == d["items"][1]["itemId"]
    # 11: 履歴
    h1, h2 = sd["history"]
    assert (h1["action"], h1["actorEmail"], h1["comment"]) == ("申請", APP1, f"{rid} から分離（元の申請日 2026-11-20）")
    assert (h2["action"], h2["actorEmail"], h2["comment"]) == ("差戻", SUP1, f"品2は不要（{rid} の明細から分離）")
    out = [h for h in res["history"] if h["action"] == "明細分離"][0]
    assert out["comment"] == f"明細「品2」を {split.request_id} として差し戻し: 品2は不要"
    assert (out["fromStep"], out["toStep"]) == ("上席", "上席")
    # 通知: 分割の差戻し（照合なし）→ 承認後の通知 → 分割後の PDF（照合あり）
    kinds = [j.kind for j in jobs(db) if j.kind != "sendApprovalRequestEmail_"]
    assert kinds == ["notifySplitReturned_", "dispatchApprovalNotifications_", "regeneratePdfAfterSplit_"]
    n = jobs(db, kind="notifySplitReturned_")[0]
    assert args(n) == [split.request_id, "上席", "品2は不要", "sup1"] and n.guard_status == ""
    assert args(jobs(db, kind="regeneratePdfAfterSplit_")[0])[1] == "購買見積"


def test_split_2_3_quote_only_remaining_prices(call, db):
    rid = create(call, items=items(4))
    to_quote(call, rid)
    d = detail(call, rid, MECH)
    prices = [{"itemId": it["itemId"], "unitPrice": 1000} for it in d["items"]]
    res = call.ok(MECH, "confirmQuote", rid, prices, "品1は別手配", refs(d, 0))
    assert [i["name"] for i in res["items"]] == ["品2", "品3", "品4"]
    assert res["request"]["totalAmount"] == (2 + 3 + 4) * 1000
    split = db.execute(select(Request).where(Request.split_from_request_id == rid)).scalar_one()
    moved = db.execute(select(RequestItem).where(RequestItem.request_id == split.request_id)).scalar_one()
    assert moved.unit_price == 0 and moved.amount == 0
    kinds = [j.kind for j in jobs(db, rid) if j.kind not in ("sendApprovalRequestEmail_", "dispatchApprovalNotifications_")]
    assert kinds == ["notifyGeneralAffairs_", "regeneratePdfAfterSplit_"]
    n = jobs(db, kind="notifySplitReturned_")[0]
    assert args(n)[1:] == ["購買見積", "品1は別手配", "mech"]


def test_split_4_gm_keeps_president_route(call, db):
    rid = create(call, items=items(2))
    to_gm(call, rid, price=40000)  # 1×4 万 + 2×4 万 = 12 万
    d = detail(call, rid, GM)
    res = call.ok(GM, "approveRequest", rid, "品2は保留", False, refs(d, 1))
    assert res["request"]["totalAmount"] == 40000
    assert res["request"]["currentStep"] == "社長"  # 経路はそのまま
    # 10: 総務部長→社長 は PDF を作らない遷移なので、分割後の作り直しを積む（実行は M）
    assert args(jobs(db, rid, "regeneratePdfAfterSplit_")[0])[1] == "社長"


def test_split_5_6_14_split_request_flows_as_pool(call, db):
    rid = create(call, items=items(2))
    d = detail(call, rid, SUP1)
    call.ok(SUP1, "approveRequest", rid, "分ける", False, refs(d, 0))
    split_id = db.execute(select(Request.request_id).where(Request.split_from_request_id == rid)).scalar_one()
    sd = detail(call, split_id, OTHER)
    assert sd["permissions"]["canEdit"] and sd["permissions"]["canDeletePool"]
    assert sd["permissions"]["canSplitItems"] is False
    res = call.ok(OTHER, "resubmitRequest", split_id)
    assert res["request"]["currentStep"] == "上席" and res["request"]["currentApproverEmail"] == SUP1
    assert detail(call, split_id, SUP1)["permissions"]["canSplitItems"] is False  # 明細 1 件


def test_split_7_all_items_or_none_is_error_and_writes_nothing(call, db):
    rid = create(call, items=items(2))
    d = detail(call, rid, SUP1)
    assert call.err(SUP1, "approveRequest", rid, "全部", False, refs(d, 0, 1)) == \
        "すべての明細を差し戻す場合は、申請全体の差戻しをご利用ください。"
    assert call.err(SUP1, "approveRequest", rid, "", False, refs(d, 0)) == \
        "差し戻す明細がある場合は、理由（コメント）を入力してください。"
    assert call.err(SUP1, "approveRequest", rid, "x", False, [{"itemId": "nope", "lineNo": 1, "name": "品1"}]) == \
        "明細の情報が画面と一致しません。他の人が明細を更新した可能性があります。画面を再読込してからやり直してください。"
    assert req(db, rid).current_step == "上席" and db.execute(select(Request)).all().__len__() == 1
    assert len(db.execute(select(RequestItem)).all()) == 2


def test_split_8_12_13_not_offered(call, db):
    rid = create(call)
    assert detail(call, rid, SUP1)["permissions"]["canSplitItems"] is False  # 明細 1 件
    rid2 = create(call, items=items(2))
    to_gm(call, rid2)
    call.ok(GM, "approveRequest", rid2, "", False)  # 購買手配へ
    d = detail(call, rid2, MECH)
    assert d["permissions"]["canSplitItems"] is False
    rid3 = create(call, items=items(2))
    d3 = detail(call, rid3, SUP1)
    assert call.err(OTHER, "approveRequest", rid3, "x", True, refs(d3, 0), "上席") == \
        "全画面モードでは明細ごとの差戻しはできません。詳細画面から操作してください。"


def test_split_9_stale_screen_after_edit(call, db):
    rid = create(call, items=items(3))
    stale = detail(call, rid, SUP1)
    edited = items(3)
    edited[1]["name"] = "品2改"
    call.ok(APP1, "updateRequestInReview", rid, {"items": edited})
    assert call.err(SUP1, "approveRequest", rid, "x", False, refs(stale, 1)) == \
        "明細の情報が画面と一致しません。他の人が明細を更新した可能性があります。画面を再読込してからやり直してください。"
    assert [i.name for i in db.execute(select(RequestItem).order_by(RequestItem.line_no)).scalars()] == [
        "品1", "品2改", "品3"]
    assert req(db, rid).current_step == "上席"


def test_split_on_president_decision(call, db):
    rid = create(call, items=items(2))
    to_gm(call, rid, price=40000)
    call.ok(GM, "approveRequest", rid, "", False)
    d = detail(call, rid, PRES)
    assert d["permissions"]["canSplitItems"] is True
    res = call.ok(PRES, "recordPresidentDecision", rid, "approve", "品1不要", False, refs(d, 0))
    assert res["request"]["currentStep"] == "購買手配" and res["request"]["totalAmount"] == 80000
    assert args(jobs(db, rid, "regeneratePdfAfterSplit_")[0])[1] == "購買手配"


# ===== 一覧・件数・ページ送り =====


def test_list_query_is_literal_and_sorted_in_sql(call, db):
    a = create(call, reasonDetail="100%_off の件")
    b = create(call, reasonDetail="1000 off")
    req(db, a).updated_at = req(db, b).updated_at  # 同じ時刻は申請 ID の新しい順
    db.commit()
    assert [r["requestId"] for r in call.ok(APP1, "getRequests", {"query": "100%_"})["rows"]] == [a]
    assert call.ok(APP1, "getRequests", {"query": "%"})["total"] == 1
    rows = call.ok(APP1, "getRequests", {"mode": "all"})["rows"]
    assert [r["requestId"] for r in rows] == sorted([a, b], reverse=True)
    assert call.ok(APP1, "getRequests", {"mode": "all", "status": "差戻し"})["total"] == 0
    # 一覧の件数は全件で数え、ページは SQL で切る
    res = call.ok(APP1, "getRequests", {"mode": "all", "pageSize": 1, "page": 2})
    assert res["total"] == 2 and len(res["rows"]) == 1 and res["summary"]["inReview"] == 2


def test_list_modes_and_badges_match(call, db):
    a = create(call)                                   # 上席（製造部）
    b = create(call, email=APP2, department="設計部")   # 上席（設計部）
    c = create(call)
    to_quote(call, c)                                  # 購買見積
    e = create(call)
    to_gm(call, e)                                     # 総務部長
    f = create(call)
    call.ok(SUP1, "returnRequest", f, "x", False, False)  # 差戻し
    g = create(call)
    call.ok(APP1, "deleteRequest", g, "")              # 取消
    for email in (ADMIN, GM, SUP1, SUP3, OTHER, MECH):
        counts = call.ok(email, "getTabCounts")
        for mode in ("supervisor", "quote", "gm", "president", "arrange", "returned", "pending"):
            res = call.ok(email, "getRequests", {"mode": mode})
            assert counts[mode] == res["total"], (email, mode)
    sup = lambda email: {r["requestId"] for r in call.ok(email, "getRequests", {"mode": "supervisor"})["rows"]}  # noqa: E731
    assert sup(ADMIN) == {a, b} and sup(GM) == {a, b}       # 管理者・総務部長は全部署
    assert sup(SUP1) == {a} and sup(SUP3) == {b}            # 上席は自分の部署だけ
    assert sup(OTHER) == set()
    # 取消は mine と all だけに出る
    assert g in {r["requestId"] for r in call.ok(APP1, "getRequests", {})["rows"]}
    assert g in {r["requestId"] for r in call.ok(OTHER, "getRequests", {"mode": "all"})["rows"]}
    assert call.ok(OTHER, "getRequests", {"mode": "returned"})["total"] == 1
    assert call.ok(APP1, "getRequests", {"mode": "mine", "status": "取消"})["total"] == 1
    pending_mech = {r["requestId"] for r in call.ok(MECH, "getRequests", {"mode": "pending"})["rows"]}
    assert pending_mech == {c}


def test_list_search_paging_and_summary(call, db):
    ids = [create(call, reasonDetail=f"理由{i}", items=[{"name": f"特殊品{i}", "quantity": 1}]) for i in range(5)]
    to_gm(call, ids[0], price=99999)        # 税込 109998
    to_gm(call, ids[1], price=1)            # 税込 1（0.1 → 0）
    to_gm(call, ids[2], price=10)
    call.ok(GM, "approveRequest", ids[2], "", False)
    call.ok(MECH, "arrangeComplete", ids[2], "")  # 完了は除く
    to_gm(call, ids[3], price=500)
    call.ok(APP1, "deleteRequest", ids[3], "")    # 取消も除く（決定事項）
    res = call.ok(APP1, "getRequests", {"mode": "mine", "page": 1, "pageSize": 2})
    assert res["total"] == 5 and res["page"] == 1 and res["pageSize"] == 2 and len(res["rows"]) == 2
    assert res["summary"] == {"inReview": 3, "returned": 0, "openAmount": 109998 + 1}
    assert res["rows"][0]["requestId"] == ids[3]  # 更新の新しい順
    page3 = call.ok(APP1, "getRequests", {"mode": "mine", "page": 3, "pageSize": 2})
    assert len(page3["rows"]) == 1
    assert call.ok(APP1, "getRequests", {"pageSize": 9999})["pageSize"] == 200
    assert call.ok(APP1, "getRequests", {"query": "理由4"})["total"] == 1
    assert call.ok(APP1, "getRequests", {"query": "特殊品4"})["total"] == 0  # 品名は検索の対象外
    assert call.ok(APP1, "getRequests", {"query": ids[1].lower()})["total"] == 1


# ===== 詳細・PDF =====


def test_detail_permissions_titles_and_stamp_names(call, db):
    rid = create(call)
    to_gm(call, rid)
    d = detail(call, rid, GM)
    p = d["permissions"]
    assert p["canApprove"] and p["canReturn"] and p["canTabletApprove"] and not p["canQuote"] and p["canRecall"] is True
    # GAS 版どおり、役職は当たった行（製造部の行）から取る。* では補わない。
    assert d["stepTitles"] == {"総務部長": "", "社長": "", "購買手配": "", "上席": "課長"}
    assert d["purchasingStampName"] == "五十嵐"
    # 段 → 押印の氏名（PDF と同じ Masters.stamp_name）。申請者は申請者氏名、購買見積はメカ購買の氏名
    assert d["stampNames"]["上席"] == "上田 一" and d["stampNames"]["購買見積"] == "五十嵐"
    assert d["stampNames"]["申請者"] == "申請 花子"
    assert "総務部長" not in d["stampNames"]  # 今の段はまだ押さない
    assert d["history"][0]["fromStatusLabel"] == "" and d["history"][-1]["toStepLabel"] == "総務部長"
    assert call.err(OTHER, "getRequestDetail", "REQ-none") == "申請が見つかりません。"


def test_generate_pdf_only_completed_and_queued(call, db):
    rid = create(call)
    assert call.err(APP1, "generateRequestPdf", rid) == "完了済み申請のみPDFを作成できます。"
    to_gm(call, rid)
    call.ok(GM, "approveRequest", rid, "", False)
    call.ok(MECH, "arrangeComplete", rid, "")
    assert call.err(OTHER, "generateRequestPdf", rid) == "完了済み申請のみPDFを作成できます。"  # 関与していない
    before = len(jobs(db, rid))
    d = call.ok(APP1, "generateRequestPdf", rid)
    job = jobs(db, rid)[-1]
    assert len(jobs(db, rid)) == before + 1 and job.kind == "regeneratePdf"
    assert args(job) == [rid, {"email": APP1, "name": "app1"}] and (job.guard_status, job.guard_step) == ("完了", "完了済")
    assert d["pendingJobs"] >= 1


def test_pending_jobs_count_drops_when_done(call, db):
    rid = create(call)
    assert detail(call, rid)["pendingJobs"] == 2
    for j in jobs(db, rid):
        j.state = "done"
    db.commit()
    assert detail(call, rid)["pendingJobs"] == 0


def test_double_submit_with_same_key_creates_one(call, db):
    first = call.ok(APP1, "createRequest", payload(), key="key-0123456789")
    second = call.ok(APP1, "createRequest", payload(), key="key-0123456789")
    assert first == second and len(db.execute(select(Request)).all()) == 1


# ===== getBootstrap・管理画面 =====


def test_bootstrap_shape_admin_and_non_admin(call, db):
    b = call.ok(ADMIN, "getBootstrap")
    assert b["appName"] == "貯蔵品購入申請" and b["version"] == "1.0.0" and b["isAdmin"] is True
    assert b["user"] == {"email": ADMIN, "name": "imaizumi"}
    assert b["settings"] == {"thresholdAmount": 100000, "pdfFolderId": "", "adminEmails": ADMIN,
                             "enableEmailNotifications": True}
    assert b["unitOptions"][0] == "個" and b["defaultUnit"] == "個" and b["unitOther"] == "__other__"
    assert b["stepLabels"]["購買見積"] == "購買（見積）" and len(b["reasons"]) == 6
    assert b["departments"] == ["*", "製造部", "設計部"] and len(b["approvers"]) == 3
    assert b["deptSupervisors"][1]["notifyEmail"] == "sup2-notify" + D
    assert b["recipients"] == [{"type": "総務部", "email": "ga" + D, "name": "総務", "active": True, "sendAs": "TO"}]
    assert b["delegate"] == {"granted": False, "email": "", "grantedAt": ""}
    assert b["mailRedirect"] == {"active": True, "to": "imaizumi" + D, "until": "", "forced": True}  # staging
    assert b["syncStatus"]["workers"] == {"lastSyncedAt": "", "ok": False, "count": 0, "error": ""}
    assert b["myApproverRule"]["department"] == "*"
    nb = call.ok(OTHER, "getBootstrap")
    assert nb["isAdmin"] is False and nb["approvers"] == [] and nb["recipients"] == [] and nb["delegate"] is None
    # 管理者以外には管理者の一覧と PDF の保存先を返さない
    db.add(Setting(key="pdfFolderId", value="FOLDER-1"))
    db.commit()
    assert call.ok(ADMIN, "getBootstrap")["settings"]["pdfFolderId"] == "FOLDER-1"
    assert call.ok(OTHER, "getBootstrap")["settings"] == {
        "thresholdAmount": 100000, "pdfFolderId": "", "adminEmails": "", "enableEmailNotifications": True}


def test_revoke_sessions_admin_only(call, db, settings):
    from fastapi.responses import Response

    from app.auth import create_session
    from app.models import UserSession

    create_session(db, Response(), OTHER, settings)
    create_session(db, Response(), OTHER, settings)
    create_session(db, Response(), APP1, settings)
    assert call.err(OTHER, "revokeSessions", OTHER) == "管理者のみ実行できます。"
    assert call.err(ADMIN, "revokeSessions", "") == "メールアドレスを入力してください。"
    assert call.ok(ADMIN, "revokeSessions", " Other" + D) == {"email": OTHER, "revoked": 2}
    assert [r.email for r in db.query(UserSession).all()] == [APP1]


def test_admin_list_empty_does_not_make_everyone_admin(call, db):
    db.get(Setting, "adminEmails").value = ""
    db.commit()
    assert call.ok(OTHER, "getBootstrap")["isAdmin"] is False
    assert call.ok(ADMIN, "getBootstrap")["isAdmin"] is True  # BOOTSTRAP_ADMINS は常に管理者
    assert call.err(OTHER, "saveSettings", {}) == "管理者のみ実行できます。"


def test_save_admin_all_is_one_transaction(call, db):
    approvers = [{"department": "*", "generalManagerEmail": "New@X.info; gm@x.info,new@x.info", "active": True},
                 {"department": "", "applicantEmail": ""}]
    bad = call.err(ADMIN, "saveAdminAll", approvers, [{"type": "経理", "email": "a@x"}], {})
    assert bad == "通知宛先の区分が不正です: 経理"
    assert len(db.execute(select(ApproverRule)).all()) == 3  # 何も変わらない
    assert call.err(ADMIN, "saveAdminAll", approvers, [{"type": "購買", "email": " "}], {}) == \
        "通知宛先のメールアドレスを入力してください。"
    b = call.ok(ADMIN, "saveAdminAll", approvers,
                [{"type": "購買", "email": "P@X.info", "sendAs": "cc", "active": False}],
                {"thresholdAmount": "150000", "pdfFolderId": " fid ", "adminEmails": f"{ADMIN}; {OTHER}",
                 "enableEmailNotifications": False})
    assert b["approvers"] == [dict(b["approvers"][0], generalManagerEmail="new@x.info, gm@x.info")]
    assert b["recipients"] == [{"type": "購買", "email": "p@x.info", "name": "", "active": False, "sendAs": "CC"}]
    assert b["settings"] == {"thresholdAmount": 150000, "pdfFolderId": "fid", "adminEmails": f"{ADMIN},{OTHER}",
                             "enableEmailNotifications": False}
    assert call.ok(OTHER, "getBootstrap")["isAdmin"] is True


def test_save_settings_threshold_rules(call, db):
    assert call.ok(ADMIN, "saveSettings", {"thresholdAmount": ""})["settings"]["thresholdAmount"] == 100000
    assert db.get(Setting, "thresholdAmount").value == "100000"
    call.ok(ADMIN, "saveSettings", {"thresholdAmount": "-5", "adminEmails": ADMIN})
    assert db.get(Setting, "thresholdAmount").value == "1"


def test_mail_redirect_in_production(db, seeded):
    prod = make_settings(app_env="production")
    c = Caller(db, prod)
    assert c.ok(ADMIN, "getBootstrap")["mailRedirect"] == {"active": False, "to": "", "until": "", "forced": False}
    m = c.ok(ADMIN, "setMailRedirect", 30)
    assert m["active"] is True and m["to"] == ADMIN and m["forced"] is False and len(m["until"]) == 16
    assert db.get(Setting, "mailRedirectTo").value.startswith(ADMIN + "|")
    assert c.ok(ADMIN, "clearMailRedirect") == {"active": False, "to": "", "until": "", "forced": False}
    assert c.err(OTHER, "setMailRedirect", 30) == "管理者のみ実行できます。"


def test_failed_jobs_list_and_retry(call, db):
    rid = create(call)
    job = jobs(db, rid)[0]
    job.state, job.attempts, job.last_error = "failed", 4, "Gmail 500"
    db.commit()
    assert call.err(OTHER, "listFailedJobs") == "管理者のみ実行できます。"
    rows = call.ok(ADMIN, "listFailedJobs")
    assert len(rows) == 1 and rows[0]["jobId"] == str(job.id) and rows[0]["requestId"] == rid
    assert rows[0]["error"] == "Gmail 500" and rows[0]["label"]
    assert call.ok(ADMIN, "retryFailedJob", str(job.id))["state"] == "pending"
    db.expire_all()
    assert db.get(Job, job.id).state == "pending" and db.get(Job, job.id).attempts == 0
    assert call.err(ADMIN, "retryFailedJob", str(job.id)) == "やり直せる後処理が見つかりません。"


class FakeApi:
    def __init__(self, tabs, gids=None):
        self.tabs, self.gids = tabs, gids or {}

    def get_spreadsheet(self, sid):
        return {"sheets": [{"properties": {"title": t, "sheetId": self.gids.get(t, i)}} for i, t in enumerate(self.tabs)]}

    def get_values(self, sid, a1):
        title = a1.split("!")[0].strip("'")
        values = self.tabs[title]
        return values[1:] if "!A2" in a1 else values


def test_sync_now_replaces_tables_and_records(call, db, monkeypatch):
    from app.domain import admin
    from app.services import sync

    assert call.err(OTHER, "syncWorkersNow") == "管理者のみ実行できます。"
    # 委任がまだ無い
    res = call.ok(ADMIN, "syncWorkersNow")
    assert res["count"] == 0 and "許可" in res["error"]
    api = FakeApi({"作業員": [["コード"], ["0012", "山田", "製造部", "", "本社", "社員"]],
                   "DeptApprovers": [["部署", "承認者メール", "氏名"], ["製造部", "a@x, b@x", "上田"]]},
                  gids={"作業員": sync.MASTER_WORKER_GID})
    monkeypatch.setattr(admin, "_sheets_api", lambda db_, s: api)
    assert call.ok(ADMIN, "syncWorkersNow") == {"count": 1}
    assert [w.worker_code for w in db.execute(select(Worker)).scalars()] == ["0012"]
    assert call.ok(ADMIN, "syncDeptSupervisorsNow") == {"count": 2}
    assert [s.email for s in db.execute(select(DeptSupervisor).order_by(DeptSupervisor.sort_order)).scalars()] == [
        "a@x", "b@x"]
    b = call.ok(ADMIN, "getBootstrap")
    assert b["workers"] == [{"code": "0012", "name": "山田", "dept": "製造部", "location": "本社", "staffType": "社員"}]
    assert b["syncStatus"]["workers"]["ok"] is True and b["syncStatus"]["workers"]["count"] == 1
    # 元が空なら上書きしない
    api.tabs["作業員"] = [["コード"]]
    assert call.ok(ADMIN, "syncWorkersNow") == {"count": 0, "skipped": True}
    assert len(db.execute(select(Worker)).all()) == 1
    assert len(db.execute(select(SyncRun)).all()) == 4


def test_history_order_is_stable_within_same_second(call, db):
    rid = create(call, items=items(2))
    d = detail(call, rid, SUP1)
    res = call.ok(SUP1, "approveRequest", rid, "分割", False, refs(d, 0))
    assert [h["action"] for h in res["history"]] == ["申請", "明細分離", "承認"]
    assert [h.action for h in db.execute(select(History).where(History.request_id == rid)
                                         .order_by(History.happened_at, History.seq)).scalars()] == [
        "申請", "明細分離", "承認"]


def test_desired_delivery_date_is_a_date_in_db(call, db):
    rid = create(call)
    item = db.execute(select(RequestItem).where(RequestItem.request_id == rid)).scalar_one()
    assert item.desired_delivery_date == date(2026, 12, 1)
    assert STATUS.IN_REVIEW == "承認中" and STEPS.DONE == "完了済"
