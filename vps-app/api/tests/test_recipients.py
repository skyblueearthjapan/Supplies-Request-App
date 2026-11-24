"""メール 17 種の To / Cc / 件名 / 本文が GAS 版と同じになること。

期待値は GAS 版 Email.gs・Workflow.gs のコードを読んで手で作った（実装の出力を写したものではない）。
"""

from __future__ import annotations

from mailpdf_fixtures import ORIGIN, make_items, make_masters, make_request

from app.services.recipients import (
    MailBuilder,
    MailKind,
    all_recipients_split,
    approver_salutation,
    job_failure_admin_mail,
    recipients_split,
    resolve_supervisors,
)

RID = "REQ-20261124093000-123456"
URL = ORIGIN + "/?requestId=" + RID
APPLICANT_LINE = "申請者: 申請 太郎（applicant@lineworks-local.info）"

OVER_BANNER = "\n".join([
    "━━━━━━━━━━━━━━━━━━━━━━",
    "🚨🚨🚨  要・社長決裁  🚨🚨🚨",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "確定金額（税抜）¥120,000 は社長決裁しきい値（税抜 ¥100,000）以上です。",
    "総務部長の承認後、必ず【社長の承認】が必要です。至急ご対応ください。",
    "━━━━━━━━━━━━━━━━━━━━━━",
])
EXPEDITE_BANNER = "\n".join([
    "━━━━━━━━━━━━━━━━━━━━━━",
    "⚡⚡⚡ 至急承認のお願い（金額確定不要）⚡⚡⚡",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "この申請は金額確定が不要な標準品（価格は商社と確定済み）です。",
    "生産を止めないため、購買にて至急手配を開始したい案件です。",
    "金額確定は行いません。総務部長の至急承認をお願いします。",
    "━━━━━━━━━━━━━━━━━━━━━━",
])
GM = {"email": "gm1@lineworks-local.info", "name": "辻野 一郎（総務部長）", "title": "部長"}


def builder(**kw):
    return MailBuilder(make_masters(**kw), ORIGIN)


def one(mails):
    assert len(mails) == 1, mails
    return mails[0]


# 1. 承認依頼（上席。通知メールは Cc、To と同じなら付けない）
def test_approval_request_supervisor_with_notify_cc():
    b = builder()
    sups = resolve_supervisors(b.masters, "applicant@lineworks-local.info", "製造部")
    assert [s["email"] for s in sups] == ["sup1@lineworks-local.info", "sup2@lineworks-local.info"]  # 無効の行は除く
    mails = []
    for s in sups:
        mails += b.approval_request(make_request(), s)
    m1, m2 = mails
    assert m1.kind == MailKind.APPROVAL_REQUEST
    assert m1.to == ["sup1@lineworks-local.info"]
    assert m1.cc == ["sup1-phone@lineworks-local.info"]
    assert m1.subject == "[貯蔵品購入申請] 承認依頼 " + RID
    assert m1.body == "\n".join([
        "上席 上田課長", "",
        "貯蔵品購入申請の承認依頼があります。", "",
        "申請番号: " + RID,
        APPLICANT_LINE,
        "部署: 製造部",
        "確定金額: 未確定（購買確定前）",
        "現在ステップ: 上席", "",
        URL,
    ])
    assert m1.attach_pdf is False
    # 氏名が空の上席は「上席」になり、宛名は「上席 上席 様」（GAS 版のまま）。通知メールが To と同じなら Cc なし。
    assert m2.to == ["sup2@lineworks-local.info"] and m2.cc == []
    assert m2.body.splitlines()[0] == "上席 上席 様"


# 2. 承認依頼（見積確定で 10 万以上: ★の件名と要社長決裁の帯）＋ 10. 総務部への金額確定（要社長決裁）
def test_quote_confirmed_over():
    req = make_request(currentStep="総務部長", totalAmount=120000, currentApproverEmail="gm1@lineworks-local.info")
    ga, ap = builder().quote_confirmed(req, GM, "¥120,000（税抜）", 120000, 100000, True)
    assert ga.kind == MailKind.GA_QUOTE_CONFIRMED_OVER
    assert ga.to == ["ga1@lineworks-local.info"] and ga.cc == ["ga2@lineworks-local.info"]
    assert ga.subject == "【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 " + RID + "（税抜10万円以上）"
    assert ga.body == "\n".join([
        "総務 辻野部長", "",
        OVER_BANNER + "\n\n購買が見積金額 ¥120,000（税抜） を確定しました。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "確定金額: ¥132,000（税込）",
        "現在ステップ: 総務部長", "", URL,
    ])
    assert ap.kind == MailKind.APPROVAL_REQUEST_OVER
    assert ap.to == ["gm1@lineworks-local.info"] and ap.cc == []  # 代表 1 人だけ（GAS 版の癖を残す）
    assert ap.subject == "【★至急・要社長決裁★】[貯蔵品購入申請] 承認依頼 " + RID
    assert ap.body == "\n".join([
        "総務 辻野部長", "", OVER_BANNER, "",
        "貯蔵品購入申請の承認依頼があります。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "確定金額: ¥132,000（税込）", "現在ステップ: 総務部長", "", URL,
    ])


# 11. 総務部への金額確定（通常）＋ 承認依頼（通常）
def test_quote_confirmed_normal():
    req = make_request(currentStep="総務部長", totalAmount=50000)
    ga, ap = builder().quote_confirmed(req, GM, "¥50,000（税抜）", 50000, 100000, False)
    assert ga.kind == MailKind.GA_QUOTE_CONFIRMED
    assert ga.subject == "[貯蔵品購入申請] 金額確定 " + RID
    assert ga.body.splitlines()[2] == "購買が見積金額 ¥50,000（税抜） を確定しました。総務部長承認をお願いします。"
    assert "確定金額: ¥55,000（税込）" in ga.body
    assert ap.kind == MailKind.APPROVAL_REQUEST and ap.subject == "[貯蔵品購入申請] 承認依頼 " + RID
    assert ap.body.splitlines()[2] == "貯蔵品購入申請の承認依頼があります。"


# 3. 承認依頼（至急）＋ 12. 総務部への至急承認
def test_expedited():
    req = make_request(currentStep="総務部長", amountWaived="true")
    ga, ap = builder().expedited(req, GM, "  CO2 ボンベ  ")
    assert ga.kind == MailKind.GA_EXPEDITED
    assert ga.subject == "【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] " + RID
    assert ga.body == "\n".join([
        "総務 辻野部長", "",
        EXPEDITE_BANNER + "\n\n購買より、金額確定不要・至急手配の申請です。\n備考: CO2 ボンベ", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "確定金額: 金額確定不要（至急手配）", "現在ステップ: 総務部長", "", URL,
    ])
    assert ap.kind == MailKind.APPROVAL_REQUEST_EXPEDITE
    assert ap.subject == "【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] 承認依頼 " + RID
    assert ap.body.startswith("総務 辻野部長\n\n" + EXPEDITE_BANNER + "\n\n貯蔵品購入申請の承認依頼があります。")


# 4. 見積依頼（メカ: To=購買の To、Cc=購買の Cc ＋ 電気購買の全員。PDF 付き）
def test_quote_request_mech():
    m = one(builder().quote_request(make_request(currentStep="購買見積")))
    assert m.kind == MailKind.QUOTE_REQUEST and m.attach_pdf is True
    assert m.to == ["mech@lineworks-local.info"]
    assert m.cc == ["mech-cc@lineworks-local.info", "applicant@lineworks-local.info",
                    "elec@lineworks-local.info", "ga2@lineworks-local.info"]
    assert m.subject == "[貯蔵品購入申請] 見積依頼 " + RID
    assert m.body == "\n".join([
        "五十嵐 様", "",
        "見積をお願いします。金額確定後、アプリ内で各明細の単価をご入力ください。",
        "押印済の申請書PDFを添付します。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "品目区分: メカ", "確定金額: 未確定（購買確定前）", "", URL,
    ])


# 5. 手配依頼（電気: To=電気購買、Cc=購買の全員。宛名は To の表示名だけ）
def test_purchasing_request_elec():
    req = make_request(currentStep="購買手配", category="電気", totalAmount=3600)
    m = one(builder().purchasing_request(req))
    assert m.kind == MailKind.PURCHASING_REQUEST and m.attach_pdf is True
    assert m.to == ["elec@lineworks-local.info", "ga2@lineworks-local.info"]
    assert m.cc == ["mech@lineworks-local.info", "mech-cc@lineworks-local.info", "applicant@lineworks-local.info"]
    assert m.subject == "[貯蔵品購入申請] 手配依頼 " + RID
    assert m.body == "\n".join([
        "電田 様", "",
        "押印済の申請書PDFを添付します。手配をお願いします。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "品目区分: 電気", "確定金額: ¥3,960（税込）", "", URL,
    ])


def test_purchasing_fallback_to_approver_master_and_generic_salutation():
    # 通知宛先が空 → 承認者マスタ（ここでは '*' 行に当たる部署）の購買を To、電気購買を Cc
    b = builder(recipients=[])
    m = one(b.quote_request(make_request(department="総務課")))
    assert m.to == ["mech@lineworks-local.info"] and m.cc == ["elec@lineworks-local.info"]
    assert m.body.splitlines()[0] == "購買 ご担当者 様"
    # 部署の行に購買が無いと、承認者マスタの補いは効かない（GAS の resolvePurchasingEmail_ は '*' を見ない）→ 送らない
    assert b.quote_request(make_request(department="製造部")) == []


def test_purchasing_cc_only_is_promoted_to_to():
    b = builder(recipients=[{"type": "購買", "email": "x@lineworks-local.info", "name": "X", "active": "", "sendAs": "CC"}])
    m = one(b.quote_request(make_request()))
    assert m.to == ["x@lineworks-local.info"] and m.cc == []
    assert m.body.splitlines()[0] == "購買 ご担当者 様"  # Cc の表示名は宛名に使わない


# 6. 差戻し（申請者）
def test_returned():
    m = one(builder().returned(make_request(status="差戻し", currentStep="申請者"), "数量を見直してください"))
    assert m.kind == MailKind.RETURNED
    assert m.to == ["applicant@lineworks-local.info"] and m.cc == []
    assert m.subject == "[貯蔵品購入申請] 差戻し " + RID
    assert m.body == "\n".join([
        "申請 太郎 様", "", "貯蔵品購入申請が差戻しされました。", "",
        "申請番号: " + RID, "コメント: 数量を見直してください", "", URL,
    ])


# 7. 差戻し共有（通知宛先の全員。To 優先・申請者を除く・明細付き）
def test_returned_broadcast():
    b = builder()
    to, cc = all_recipients_split(b.masters)
    assert to == ["ga1@lineworks-local.info", "mech@lineworks-local.info", "elec@lineworks-local.info", "ga2@lineworks-local.info"]
    assert cc == ["mech-cc@lineworks-local.info", "applicant@lineworks-local.info"]
    req = make_request(totalAmount=3600)
    m = one(b.returned_broadcast(req, make_items(), "上席", "理由です", "上田 一郎"))
    assert m.kind == MailKind.RETURNED_BROADCAST
    assert m.to == to
    assert m.cc == ["mech-cc@lineworks-local.info"]  # 申請者は除く
    assert m.subject == "[貯蔵品購入申請] 差戻し共有 " + RID
    assert m.body == "\n".join([
        "申請 太郎 さんが申請した貯蔵品購入申請が、上席承認の段階で差戻しになりました。", "",
        "申請番号: " + RID,
        "申請者　: 申請 太郎（applicant@lineworks-local.info）",
        "部署　　: 製造部",
        "差戻し段階: 上席承認",
        "差戻し者: 上田 一郎",
        "理由　　: 理由です",
        "確定金額: ¥3,960（税込）", "",
        "― 申請明細（品名／型式／数量／単価(税抜)／金額(税抜)） ―",
        "1. ウエス／型式: W-100／数量: 3 ケース／単価: ¥1,200／金額: ¥3,600",
        "2. オイル／型式: —／数量: 1.5 L／単価: 未定／金額: 未定", "",
        "小計（税抜）　: ¥3,600",
        "消費税（10%） : ¥360",
        "合計（税込）　: ¥3,960", "",
        URL,
    ])


def test_returned_broadcast_edge_cases():
    b = builder(recipients=[{"type": "総務部", "email": "only-cc@lineworks-local.info", "name": "", "active": "", "sendAs": "CC"}])
    m = one(b.returned_broadcast(make_request(department="", applicantName=""), [], "購買見積", "", ""))
    assert m.to == ["only-cc@lineworks-local.info"] and m.cc == []  # Cc だけなら To へ上げる
    lines = m.body.splitlines()
    assert lines[0] == "（不明） さんが申請した貯蔵品購入申請が、購買（見積・金額入力）の段階で差戻しになりました。"
    assert "部署　　: （未設定）" in lines and "理由　　: （コメントなし）" in lines and "（明細なし）" in lines
    assert not any(line.startswith("差戻し者") for line in lines)
    # 申請者しかいなければ送らない
    b2 = builder(recipients=[{"type": "購買", "email": "applicant@lineworks-local.info", "name": "", "active": "", "sendAs": "TO"}])
    assert b2.returned_broadcast(make_request(), [], "上席", "x", "y") == []


# 8. 却下
def test_rejected():
    m = one(builder().rejected(make_request(status="取消"), "不要になった"))
    assert m.kind == MailKind.REJECTED and m.to == ["applicant@lineworks-local.info"]
    assert m.subject == "[貯蔵品購入申請] 却下 " + RID
    assert m.body == "\n".join([
        "申請 太郎 様", "",
        "貯蔵品購入申請が却下されました。この申請は取消となり、再提出はできません。",
        "再度申請が必要な場合は、新規申請を作成してください。", "",
        "申請番号: " + RID, "理由: 不要になった", "", URL,
    ])


# 9. 内容更新（上席の段は上席マスタの全員、1 人 1 通、編集した本人を除く）
def test_request_updated_supervisor_excludes_editor():
    mails = builder().request_updated(make_request(), "明細1 数量: 2→3", "上席 二郎", "SUP2@lineworks-local.info")
    m = one(mails)
    assert m.kind == MailKind.REQUEST_UPDATED
    assert m.to == ["sup1@lineworks-local.info"] and m.cc == []  # 通知メールの Cc は付けない（GAS 版と同じ）
    assert m.subject == "[貯蔵品購入申請] 内容更新 " + RID
    assert m.body == "\n".join([
        "上席 ご担当者 様", "",
        "上席 二郎 さんが承認前の申請内容（明細）を修正しました。お手数ですが、最新の内容をご確認のうえ承認をお願いします。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部", "現在ステップ: 上席", "",
        "― 変更内容 ―", "明細1 数量: 2→3", "",
        URL,
    ])


def test_request_updated_role_step_one_mail_each():
    req = make_request(currentStep="購買見積", currentApproverEmail="mech@lineworks-local.info, elec@lineworks-local.info")
    mails = builder().request_updated(req, "", "", "someone@lineworks-local.info")
    assert [m.to for m in mails] == [["mech@lineworks-local.info"], ["elec@lineworks-local.info"]]
    body = mails[0].body.splitlines()
    assert body[0] == "購買 五十嵐係長"  # 部署の行が空なので '*' の氏名・役職（カッコ書きは除く）
    assert body[2].startswith("担当者 さんが")
    assert "― 変更内容 ―" not in body
    assert builder().request_updated(make_request(currentApproverEmail=""), "x", "a", "b") == []


# 13. 総務部への手配完了（PDF 付き）＋ 14. 完了（申請者。本文に PDF の URL、空行は落ちる）
def test_arranged_and_completed():
    req = make_request(status="完了", currentStep="完了済", totalAmount=3600, pdfUrl="https://drive.google.com/file/d/F1/view")
    ga, done = builder().arranged_and_completed(req)
    assert ga.kind == MailKind.GA_ARRANGED and ga.attach_pdf is True
    assert ga.to == ["ga1@lineworks-local.info"] and ga.cc == ["ga2@lineworks-local.info"]
    assert ga.subject == "[貯蔵品購入申請] 手配完了 " + RID
    assert ga.body == "\n".join([
        "総務 辻野部長", "", "貯蔵品購入申請の手配が完了しました。", "",
        "申請番号: " + RID, APPLICANT_LINE, "部署: 製造部",
        "確定金額: ¥3,960（税込）", "現在ステップ: 完了", "", URL,
    ])
    assert done.kind == MailKind.COMPLETED and done.attach_pdf is False
    assert done.to == ["applicant@lineworks-local.info"]
    assert done.subject == "[貯蔵品購入申請] 完了 " + RID
    assert done.body == "\n".join([
        "申請 太郎 様", "貯蔵品購入申請が完了しました。",
        "申請番号: " + RID, "確定金額: ¥3,960（税込）",
        "PDF: https://drive.google.com/file/d/F1/view", URL,
    ])


def test_general_affairs_fallback_to_general_manager():
    b = builder(recipients=[])
    ga, _ = b.arranged_and_completed(make_request(department="総務課", status="完了", currentStep="完了済"))
    assert ga.to == ["gm1@lineworks-local.info", "gm2@lineworks-local.info"] and ga.cc == []
    # 部署の行に総務部長が無ければ（'*' は見ない）送らない。完了メールだけ。
    mails = b.arranged_and_completed(make_request(status="完了", currentStep="完了済"))
    assert [m.kind for m in mails] == [MailKind.COMPLETED]


# 15. 後処理失敗の管理者通知（通知の停止を見ない）
def test_admin_failure_ignores_disable_setting():
    settings = {"enableEmailNotifications": False, "adminEmails": "a@lineworks-local.info, b@lineworks-local.info"}
    m = one(job_failure_admin_mail(settings, "approval_dispatch", RID, "boom", ["imaizumi@lineworks-local.info", "a@lineworks-local.info"]))
    assert m.kind == MailKind.ADMIN_JOB_FAILURE
    assert m.to == ["a@lineworks-local.info", "b@lineworks-local.info", "imaizumi@lineworks-local.info"]
    assert m.subject == "[貯蔵品購入申請] 通知・PDFの後処理に失敗しました " + RID
    assert "処理: approval_dispatch" in m.body and "申請: " + RID in m.body and "エラー: boom" in m.body
    assert job_failure_admin_mail({"adminEmails": ""}, "x", RID, "e") == []


# 16-17. 分割の差戻し（切り出した申請の差戻し＋差戻し共有）
def test_split_returned():
    new_req = make_request(requestId="REQ-20261124100000-000001", status="差戻し", currentStep="申請者")
    returned, broadcast = builder().split_returned(new_req, make_items(), "総務部長", "分けて", "辻野 一郎")
    assert returned.kind == MailKind.SPLIT_RETURNED
    assert returned.subject == "[貯蔵品購入申請] 差戻し REQ-20261124100000-000001"
    assert broadcast.kind == MailKind.SPLIT_RETURNED_BROADCAST
    assert broadcast.subject == "[貯蔵品購入申請] 差戻し共有 REQ-20261124100000-000001"
    assert "総務部長承認の段階で差戻しになりました。" in broadcast.body


# 次の段の全員へ（総務部長・社長は承認者マスタの全員、'*' で補う。誰もいなければ代表）
def test_approval_requests_for_step_all_members_and_fallback():
    b = builder()
    req = make_request(currentStep="総務部長")
    assert [m.to for m in b.approval_requests_for_step(req, "総務部長")] == [["gm1@lineworks-local.info"], ["gm2@lineworks-local.info"]]
    b2 = builder(approver_rules=[])
    fb = {"email": "rep@lineworks-local.info", "name": "代表"}
    assert [m.to for m in b2.approval_requests_for_step(req, "総務部長", fb)] == [["rep@lineworks-local.info"]]
    # 上席の段は上席マスタの全員（Cc の通知メール付き）
    sup = b.approval_requests_for_step(make_request(), "上席")
    assert [m.to for m in sup] == [["sup1@lineworks-local.info"], ["sup2@lineworks-local.info"]]
    # 上席マスタに部署が無ければ '*'
    star = b.approval_requests_for_step(make_request(department="経理部"), "上席")
    assert [m.to for m in star] == [["star-sup@lineworks-local.info"]]


def test_recall_requests_purchasing_order_follows_category():
    b = builder()
    elec = b.recall_approval_requests(make_request(currentStep="購買見積", category="電気"), "購買見積")
    assert [m.to[0] for m in elec] == ["elec@lineworks-local.info", "mech@lineworks-local.info"]
    mech = b.recall_approval_requests(make_request(currentStep="購買見積"), "購買見積")
    assert [m.to[0] for m in mech] == ["mech@lineworks-local.info", "elec@lineworks-local.info"]


def test_notifications_disabled_sends_nothing():
    for value in (False, "false", " FALSE "):
        b = builder(settings={"enableEmailNotifications": value})
        req = make_request()
        assert b.approval_request(req, {"email": "x@lineworks-local.info"}) == []
        assert b.returned(req, "c") == [] and b.rejected(req, "c") == []
        assert b.returned_broadcast(req, [], "上席", "c", "a") == []
        assert b.quote_request(req) == [] and b.purchasing_request(req) == []
        assert b.arranged_and_completed(req) == []
        assert b.request_updated(req, "s", "e", "e@x") == []
    assert builder(settings={"enableEmailNotifications": ""}).returned(make_request(), "c")  # 空は有効


def test_recipient_split_first_row_wins_and_salutation_rules():
    b = builder(recipients=[
        {"type": "総務部", "email": "Dup@lineworks-local.info", "name": "", "active": "", "sendAs": "cc"},
        {"type": "総務部", "email": "dup@lineworks-local.info", "name": "", "active": "", "sendAs": "TO"},
    ])
    assert recipients_split(b.masters, "総務部") == ([], ["dup@lineworks-local.info"])  # 同じメールは先の行
    assert approver_salutation("辻野（総務部長）", "", "総務") == "総務 辻野 様"
    assert approver_salutation("", "部長", "総務") == "総務 ご担当者 様"
    assert approver_salutation("山田　花子", "課長", "上席") == "上席 山田課長"


def test_url_is_encoded_like_encodeURIComponent():
    b = builder()
    assert b.url("REQ 1/2") == ORIGIN + "/?requestId=REQ%201%2F2"
