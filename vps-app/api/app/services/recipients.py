"""メールの宛先・件名・本文の決め方（GAS 版 Email.gs / Recipients.gs と Workflow.gs の送信部分の移植）。

ここでは「何を誰に送るか」だけを決め、送信はしない（services/mail.py が送る）。
入力は申請 dict（GAS のシートの列名＝camelCase）、マスタ一式（Masters）、公開 URL。
出力は 1 通ずつの OutgoingMail のリスト。宛先が無い・通知停止中は空のリスト。

送らない操作（GAS 版と同じ。ここに関数を作らない）:
- 取消（deleteRequest）
- 差戻し中の保存（updateRequestDraft）
- PDF 再作成（generateRequestPdf）
- 見積確定・至急承認のときの申請者への連絡
- 新規申請・再申請の申請者本人への控え
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .mailpdf_common import (
    APP_NAME,
    CATEGORIES,
    RECIPIENT_TYPES,
    STEP_LABELS,
    STEPS,
    Masters,
    category_label,
    email_notifications_enabled,
    format_currency,
    gross_amount,
    js_str,
    normalize_category,
    normalize_email,
    parse_boolean,
    parse_number,
    qty_text,
    request_url,
    sanitize_text,
    split_emails,
    strip_role_paren,
    tax_amount,
)


# メールの種類（mail_log.kind に入れる値）。
class MailKind:
    APPROVAL_REQUEST = "approval_request"  # 承認依頼（通常）
    APPROVAL_REQUEST_OVER = "approval_request_over"  # 承認依頼（要社長決裁・見積確定 10 万以上）
    APPROVAL_REQUEST_EXPEDITE = "approval_request_expedite"  # 承認依頼（至急・金額確定不要）
    QUOTE_REQUEST = "quote_request"  # 見積依頼（購買・PDF 付き）
    PURCHASING_REQUEST = "purchasing_request"  # 手配依頼（購買・PDF 付き）
    RETURNED = "returned"  # 差戻し（申請者）
    RETURNED_BROADCAST = "returned_broadcast"  # 差戻し共有（通知宛先の全員）
    REJECTED = "rejected"  # 却下（申請者）
    REQUEST_UPDATED = "request_updated"  # 内容更新（現在の承認者）
    GA_QUOTE_CONFIRMED = "ga_quote_confirmed"  # 総務部: 金額確定
    GA_QUOTE_CONFIRMED_OVER = "ga_quote_confirmed_over"  # 総務部: 金額確定（要社長決裁）
    GA_EXPEDITED = "ga_expedited"  # 総務部: 至急承認
    GA_ARRANGED = "ga_arranged"  # 総務部: 手配完了（PDF 付き）
    COMPLETED = "completed"  # 完了（申請者）
    SPLIT_RETURNED = "split_returned"  # 分割で切り出した申請の差戻し（申請者）
    SPLIT_RETURNED_BROADCAST = "split_returned_broadcast"  # 分割の差戻し共有
    ADMIN_JOB_FAILURE = "admin_job_failure"  # 後処理失敗の管理者通知


@dataclass
class OutgoingMail:
    kind: str
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    attach_pdf: bool = False
    request_id: str = ""

    def to_header(self) -> str:
        return ",".join(self.to)

    def cc_header(self) -> str:
        return ",".join(self.cc)


# ---------------------------------------------------------------------------
# マスタの探し方（Workflow.gs / DeptSupervisors.gs / Recipients.gs の移植）
# ---------------------------------------------------------------------------

def find_approver_rule(masters: Masters, applicant_email: Any, department: Any) -> dict | None:
    """申請者メール → 部署 → '*' の順に、最初に当たった有効な行（findApproverRule_）。"""
    email = normalize_email(applicant_email)
    dept = sanitize_text(department, 100)
    rows = [r for r in masters.approver_rules if r["active"]]
    for r in rows:
        if r["applicantEmail"] and r["applicantEmail"] == email:
            return r
    for r in rows:
        if dept and r["department"] == dept and not r["applicantEmail"]:
            return r
    for r in rows:
        if r["department"] == "*" and not r["applicantEmail"]:
            return r
    return None


def find_star_rule(masters: Masters) -> dict | None:
    for r in masters.approver_rules:
        if r["active"] and r["department"] == "*" and not r["applicantEmail"]:
            return r
    return None


def step_role_field(step: str) -> str:
    if step == STEPS.GENERAL_MANAGER:
        return "generalManager"
    if step == STEPS.PRESIDENT:
        return "president"
    if step in (STEPS.PURCHASING, STEPS.PURCHASING_QUOTE):
        return "purchasing"
    return ""


def step_role_default_name(step: str) -> str:
    if step == STEPS.GENERAL_MANAGER:
        return "総務部長"
    if step == STEPS.PRESIDENT:
        return "社長"
    if step in (STEPS.PURCHASING, STEPS.PURCHASING_QUOTE):
        return "購買"
    return STEP_LABELS.get(step, "")


def _members_from_field(rule: Mapping, fld: str, default_name: str) -> list[dict]:
    name = rule.get(fld + "Name") or default_name or ""
    title = rule.get(fld + "Title") or ""
    return [{"email": e, "name": name, "title": title} for e in split_emails(rule.get(fld + "Email"))]


def resolve_step_approvers(rule: Mapping | None, step: str, category: Any = None) -> list[dict]:
    if not rule:
        return []
    fld = step_role_field(step)
    if not fld:
        return []
    if fld == "purchasing":
        mech = _members_from_field(rule, "purchasing", step_role_default_name(step))
        elec = _members_from_field(rule, "purchasingElec", "電気購買")
        ordered = elec + mech if normalize_category(category) == CATEGORIES.ELEC else mech + elec
        seen: set[str] = set()
        members = []
        for m in ordered:
            if m["email"] and m["email"] not in seen:
                seen.add(m["email"])
                members.append(m)
        return members
    return _members_from_field(rule, fld, step_role_default_name(step))


def resolve_step_approvers_with_star(masters: Masters, rule: Mapping | None, step: str, category: Any = None) -> list[dict]:
    approvers = resolve_step_approvers(rule, step, category)
    if approvers:
        return approvers
    if not rule or rule.get("department") != "*":
        star = find_star_rule(masters)
        if star:
            return resolve_step_approvers(star, step, category)
    return approvers


def resolve_supervisors(masters: Masters, applicant_email: Any, department: Any) -> list[dict]:
    """部署の上席（上席マスタの部署一致 → '*' → 承認者マスタの上席）。resolveSupervisors_。"""
    dept = sanitize_text(department, 100)
    active = [r for r in masters.dept_supervisors if r["active"] and r["email"]]
    matched = [r for r in active if r["department"] == dept]
    if not matched:
        matched = [r for r in active if r["department"] == "*"]
    if matched:
        return [
            {"email": r["email"], "name": r["name"] or "上席", "title": r["title"] or "", "notifyEmail": r["notifyEmail"] or ""}
            for r in matched
        ]
    rule = find_approver_rule(masters, applicant_email, department)
    if rule and rule.get("supervisorEmail"):
        return [{
            "email": normalize_email(rule["supervisorEmail"]),
            "name": rule.get("supervisorName") or "上席",
            "title": rule.get("supervisorTitle") or "",
            "notifyEmail": "",
        }]
    return []


def _active_recipients(masters: Masters, type_: str | None = None) -> list[dict]:
    return [r for r in masters.recipients if r["active"] and r["email"] and (type_ is None or r["type"] == type_)]


def recipient_emails(masters: Masters, type_: str) -> list[str]:
    """getRecipientEmails_: 種別の有効な宛先（To/Cc を区別しない・先勝ちで重複除去）。"""
    seen: set[str] = set()
    out = []
    for r in _active_recipients(masters, type_):
        if r["email"] not in seen:
            seen.add(r["email"])
            out.append(r["email"])
    return out


def recipient_to_names(masters: Masters, type_: str) -> list[str]:
    """getRecipientToNames_: To の表示名（表示名が空の行は飛ばす）。"""
    seen: set[str] = set()
    out = []
    for r in _active_recipients(masters, type_):
        if r["sendAs"] != "TO" or not r["name"]:
            continue
        if r["email"] in seen:
            continue
        seen.add(r["email"])
        out.append(r["name"])
    return out


def recipients_split(masters: Masters, type_: str) -> tuple[list[str], list[str]]:
    """getRecipientsSplit_: 種別ごとの To / Cc（同じメールは先に出た行の区分を使う）。"""
    to: list[str] = []
    cc: list[str] = []
    seen: set[str] = set()
    for r in _active_recipients(masters, type_):
        if r["email"] in seen:
            continue
        seen.add(r["email"])
        (cc if r["sendAs"] == "CC" else to).append(r["email"])
    return to, cc


def all_recipients_split(masters: Masters) -> tuple[list[str], list[str]]:
    """getAllRecipientsSplit_: 種別をまたいで To / Cc。To と Cc の両方にあれば To を優先。"""
    rows = _active_recipients(masters)
    to: list[str] = []
    to_seen: set[str] = set()
    for r in rows:
        if r["sendAs"] != "CC" and r["email"] not in to_seen:
            to_seen.add(r["email"])
            to.append(r["email"])
    cc: list[str] = []
    cc_seen: set[str] = set()
    for r in rows:
        if r["sendAs"] == "CC" and r["email"] not in to_seen and r["email"] not in cc_seen:
            cc_seen.add(r["email"])
            cc.append(r["email"])
    return to, cc


def _rule_emails(masters: Masters, request: Mapping, fld: str) -> str:
    rule = find_approver_rule(masters, request.get("applicantEmail"), request.get("department"))
    if rule and rule.get(fld):
        return ",".join(split_emails(rule[fld]))
    return ""


def resolve_general_manager_email(masters: Masters, request: Mapping) -> str:
    return _rule_emails(masters, request, "generalManagerEmail")


def resolve_purchasing_email(masters: Masters, request: Mapping) -> str:
    return _rule_emails(masters, request, "purchasingEmail")


def resolve_purchasing_elec_email(masters: Masters, request: Mapping) -> str:
    return _rule_emails(masters, request, "purchasingElecEmail")


def purchasing_types_for_category(category: Any) -> tuple[str, str]:
    """(主担当, もう一方)。電気 → 電気購買、メカ／一般・不明 → 購買（メカ）。"""
    if normalize_category(category) == CATEGORIES.ELEC:
        return RECIPIENT_TYPES.PURCHASING_ELEC, RECIPIENT_TYPES.PURCHASING
    return RECIPIENT_TYPES.PURCHASING, RECIPIENT_TYPES.PURCHASING_ELEC


def _purchasing_fallback_by_type(masters: Masters, type_: str, request: Mapping) -> str:
    if type_ == RECIPIENT_TYPES.PURCHASING_ELEC:
        return resolve_purchasing_elec_email(masters, request)
    return resolve_purchasing_email(masters, request)


# ---------------------------------------------------------------------------
# 宛名・金額の書き方
# ---------------------------------------------------------------------------

def step_role_salutation_label(step: str) -> str:
    if step == STEPS.SUPERVISOR:
        return "上席"
    if step == STEPS.GENERAL_MANAGER:
        return "総務"
    if step == STEPS.PRESIDENT:
        return "社長"
    if step in (STEPS.PURCHASING, STEPS.PURCHASING_QUOTE):
        return "購買"
    return "ご担当"


def approver_salutation(name: Any, title: Any, role_label: str) -> str:
    cleaned = strip_role_paren(name) if name else ""
    surname = cleaned.split()[0] if cleaned.split() else ""
    if surname and title:
        return f"{role_label} {surname}{title}"
    if surname:
        return f"{role_label} {surname} 様"
    return f"{role_label} ご担当者 様"


def role_name_title(masters: Masters, applicant_email: Any, department: Any, step: str) -> dict:
    """roleNameTitle_: 役職ステップの氏名・役職（部署別 → '*'。既定名は使わない）。"""
    fld = step_role_field(step)
    if not fld:
        return {"name": "", "title": ""}
    rule = find_approver_rule(masters, applicant_email, department)
    name = (rule.get(fld + "Name") or "") if rule else ""
    title = (rule.get(fld + "Title") or "") if rule else ""
    if not name:
        star = find_star_rule(masters)
        if star:
            name = star.get(fld + "Name") or name
            if not title:
                title = star.get(fld + "Title") or ""
    return {"name": name, "title": title}


def salutation_for_step(masters: Masters, request: Mapping, step: str, approver: Mapping | None) -> str:
    label = step_role_salutation_label(step)
    if step == STEPS.SUPERVISOR:
        return approver_salutation(approver.get("name") if approver else "", approver.get("title") if approver else "", label)
    nt = role_name_title(masters, request.get("applicantEmail"), request.get("department"), step)
    return approver_salutation(nt["name"], nt["title"], label)


def purchasing_salutation(masters: Masters, request: Mapping) -> str:
    primary, _ = purchasing_types_for_category(request.get("category"))
    names = recipient_to_names(masters, primary)
    if not names:
        return approver_salutation("", "", step_role_salutation_label(STEPS.PURCHASING))
    return "\n".join(names)


def amount_text(request: Mapping) -> str:
    net = parse_number(request.get("totalAmount"))
    if net > 0:
        return format_currency(gross_amount(net)) + "（税込）"
    if parse_boolean(request.get("amountWaived")):
        return "金額確定不要（至急手配）"
    return "未確定（購買確定前）"


def amount_breakdown_lines(request: Mapping) -> list[str]:
    net = parse_number(request.get("totalAmount"))
    if not net > 0:
        return []
    return [
        "小計（税抜）　: " + format_currency(net),
        "消費税（10%） : " + format_currency(tax_amount(net)),
        "合計（税込）　: " + format_currency(gross_amount(net)),
    ]


def item_money_text(value: Any) -> str:
    return format_currency(value) if parse_number(value) > 0 else "未定"


def returned_stage_phrase(step: str) -> str:
    return {
        STEPS.SUPERVISOR: "上席承認",
        STEPS.PURCHASING_QUOTE: "購買（見積・金額入力）",
        STEPS.GENERAL_MANAGER: "総務部長承認",
        STEPS.PRESIDENT: "社長決裁",
        STEPS.PURCHASING: "購買手配",
    }.get(step) or STEP_LABELS.get(step) or js_str(step)


def current_step_label(request: Mapping) -> str:
    step = request.get("currentStep") or ""
    return STEP_LABELS.get(step) or js_str(step)


# ---------------------------------------------------------------------------
# 1 種類ずつの組み立て（GAS 版の関数と 1 対 1）
# ---------------------------------------------------------------------------

class MailBuilder:
    """1 件の申請についてメールを組み立てる。masters・公開 URL を持ち回る。"""

    def __init__(self, masters: Masters, public_origin: str):
        self.masters = masters
        self.public_origin = public_origin

    @property
    def enabled(self) -> bool:
        return email_notifications_enabled(self.masters.settings)

    def url(self, request_id: str) -> str:
        return request_url(self.public_origin, request_id)

    # sendEmail_: 1 通（To は 1 つ。cc は任意）
    def _single(self, kind: str, request: Mapping, to: str, subject: str, body: str, cc: str = "") -> list[OutgoingMail]:
        if not self.enabled or not to:
            return []
        return [OutgoingMail(kind=kind, to=split_emails(to) or [to], cc=[cc] if cc else [], subject=subject, body=body,
                             request_id=js_str(request.get("requestId")))]

    # --- 承認依頼 -----------------------------------------------------------
    def approval_request(self, request: Mapping, approver: Mapping | None, extra_note: str = "",
                         subject_prefix: str = "", kind: str | None = None) -> list[OutgoingMail]:
        """sendApprovalRequestEmail_。approver は {email, name, title, notifyEmail}（上席は上席マスタ由来）。"""
        if not approver or not approver.get("email"):
            return []
        rid = js_str(request.get("requestId"))
        prefix = subject_prefix or ("【★至急・要社長決裁★】" if extra_note else "")
        subject = prefix + "[貯蔵品購入申請] 承認依頼 " + rid
        lines = [salutation_for_step(self.masters, request, request.get("currentStep") or "", approver), ""]
        if extra_note:
            lines += [extra_note, ""]
        lines += [
            "貯蔵品購入申請の承認依頼があります。",
            "",
            "申請番号: " + rid,
            "申請者: " + js_str(request.get("applicantName")) + "（" + js_str(request.get("applicantEmail")) + "）",
            "部署: " + js_str(request.get("department")),
            "確定金額: " + amount_text(request),
            "現在ステップ: " + current_step_label(request),
            "",
            self.url(rid),
        ]
        cc = ""
        if approver.get("notifyEmail"):
            notify = normalize_email(approver["notifyEmail"])
            if notify and notify != normalize_email(approver["email"]):
                cc = notify
        if kind is None:
            if subject_prefix:
                kind = MailKind.APPROVAL_REQUEST_EXPEDITE
            elif extra_note:
                kind = MailKind.APPROVAL_REQUEST_OVER
            else:
                kind = MailKind.APPROVAL_REQUEST
        return self._single(kind, request, approver["email"], subject, "\n".join(lines), cc)

    def approval_requests_for_step(self, request: Mapping, step: str, fallback: Mapping | None = None) -> list[OutgoingMail]:
        """次の段の全員へ承認依頼（dispatchApprovalNotifications_ の最後の分岐）。

        上席の段は上席マスタの全員（createRequest / resubmitRequest / recallStep と同じ）。
        役職の段は承認者マスタの全員（'*' で補う）。誰もいなければ fallback（代表）へ。
        """
        if step == STEPS.SUPERVISOR:
            members = resolve_supervisors(self.masters, request.get("applicantEmail"), request.get("department"))
        else:
            rule = find_approver_rule(self.masters, request.get("applicantEmail"), request.get("department"))
            members = resolve_step_approvers_with_star(self.masters, rule, step)
        if not members:
            members = [fallback] if fallback else []
        out: list[OutgoingMail] = []
        for m in members:
            out += self.approval_request(request, m)
        return out

    def recall_approval_requests(self, request: Mapping, step: str) -> list[OutgoingMail]:
        """recallStep: 戻し先の段の承認者（購買は品目区分で代表の並びを変える）へ再承認依頼。"""
        if step == STEPS.SUPERVISOR:
            members = resolve_supervisors(self.masters, request.get("applicantEmail"), request.get("department"))
        else:
            rule = find_approver_rule(self.masters, request.get("applicantEmail"), request.get("department"))
            members = resolve_step_approvers_with_star(self.masters, rule, step, request.get("category"))
        out: list[OutgoingMail] = []
        for m in members:
            out += self.approval_request(request, m)
        return out

    # --- 差戻し・却下 -------------------------------------------------------
    def returned(self, request: Mapping, comment: str, kind: str = MailKind.RETURNED) -> list[OutgoingMail]:
        rid = js_str(request.get("requestId"))
        body = "\n".join([
            js_str(request.get("applicantName")) + " 様",
            "",
            "貯蔵品購入申請が差戻しされました。",
            "",
            "申請番号: " + rid,
            "コメント: " + js_str(comment),
            "",
            self.url(rid),
        ])
        return self._single(kind, request, js_str(request.get("applicantEmail")), "[貯蔵品購入申請] 差戻し " + rid, body)

    def rejected(self, request: Mapping, comment: str) -> list[OutgoingMail]:
        rid = js_str(request.get("requestId"))
        body = "\n".join([
            js_str(request.get("applicantName")) + " 様",
            "",
            "貯蔵品購入申請が却下されました。この申請は取消となり、再提出はできません。",
            "再度申請が必要な場合は、新規申請を作成してください。",
            "",
            "申請番号: " + rid,
            "理由: " + js_str(comment),
            "",
            self.url(rid),
        ])
        return self._single(MailKind.REJECTED, request, js_str(request.get("applicantEmail")), "[貯蔵品購入申請] 却下 " + rid, body)

    def returned_broadcast(self, request: Mapping, items: Sequence[Mapping], step_at_return: str, comment: str,
                           actor_name: str, kind: str = MailKind.RETURNED_BROADCAST) -> list[OutgoingMail]:
        """sendReturnedBroadcast_。items は申請明細（行番号順）。"""
        if not self.enabled:
            return []
        applicant = normalize_email(request.get("applicantEmail"))
        to_all, cc_all = all_recipients_split(self.masters)
        to = [e for e in to_all if e != applicant]
        cc = [e for e in cc_all if e != applicant]
        if not to and not cc:
            return []
        if not to:
            to, cc = cc, []
        rid = js_str(request.get("requestId"))
        applicant_name = js_str(request.get("applicantName")) or "（不明）"
        stage = returned_stage_phrase(step_at_return)
        lines = [
            applicant_name + " さんが申請した貯蔵品購入申請が、" + stage + "の段階で差戻しになりました。",
            "",
            "申請番号: " + rid,
            "申請者　: " + applicant_name + "（" + js_str(request.get("applicantEmail")) + "）",
            "部署　　: " + (js_str(request.get("department")) or "（未設定）"),
            "差戻し段階: " + stage,
        ]
        if actor_name:
            lines.append("差戻し者: " + js_str(actor_name))
        lines.append("理由　　: " + (js_str(comment) or "（コメントなし）"))
        lines.append("確定金額: " + amount_text(request))
        lines.append("")
        lines.append("― 申請明細（品名／型式／数量／単価(税抜)／金額(税抜)） ―")
        if not items:
            lines.append("（明細なし）")
        else:
            ordered = sorted(items, key=lambda it: parse_number(it.get("lineNo")))
            for index, it in enumerate(ordered):
                qty = parse_number(it.get("quantity")) or 0
                lines.append(
                    f"{index + 1}. " + js_str(it.get("name"))
                    + "／型式: " + (js_str(it.get("model")) or "—")
                    + "／数量: " + qty_text(qty, it.get("unit"))
                    + "／単価: " + item_money_text(it.get("unitPrice"))
                    + "／金額: " + item_money_text(it.get("amount"))
                )
        breakdown = amount_breakdown_lines(request)
        if breakdown:
            lines.append("")
            lines += breakdown
        lines.append("")
        lines.append(self.url(rid))
        return [OutgoingMail(kind=kind, to=to, cc=cc, subject="[貯蔵品購入申請] 差戻し共有 " + rid,
                             body="\n".join(lines), request_id=rid)]

    def split_returned(self, new_request: Mapping, items: Sequence[Mapping], step_at_split: str, comment: str,
                       actor_name: str) -> list[OutgoingMail]:
        """notifySplitReturned_: 切り出した申請の差戻し（申請者）＋差戻し共有。"""
        return (self.returned(new_request, comment, kind=MailKind.SPLIT_RETURNED)
                + self.returned_broadcast(new_request, items, step_at_split, comment, actor_name,
                                          kind=MailKind.SPLIT_RETURNED_BROADCAST))

    # --- 内容更新 -----------------------------------------------------------
    def request_updated(self, request: Mapping, change_summary: str, editor_name: str, editor_email: str) -> list[OutgoingMail]:
        """sendRequestUpdatedEmail_。1 人 1 通。編集した本人は除く。"""
        if not request or not request.get("currentApproverEmail"):
            return []
        rid = js_str(request.get("requestId"))
        editor = editor_name or "担当者"
        excluded = normalize_email(editor_email)
        lines = [
            salutation_for_step(self.masters, request, request.get("currentStep") or "", None),
            "",
            editor + " さんが承認前の申請内容（明細）を修正しました。お手数ですが、最新の内容をご確認のうえ承認をお願いします。",
            "",
            "申請番号: " + rid,
            "申請者: " + js_str(request.get("applicantName")) + "（" + js_str(request.get("applicantEmail")) + "）",
            "部署: " + js_str(request.get("department")),
            "現在ステップ: " + current_step_label(request),
            "",
        ]
        if change_summary:
            lines += ["― 変更内容 ―", change_summary, ""]
        lines.append(self.url(rid))
        body = "\n".join(lines)
        if request.get("currentStep") == STEPS.SUPERVISOR:
            sups = resolve_supervisors(self.masters, request.get("applicantEmail"), request.get("department"))
            recipients = [s["email"] for s in sups] if sups else split_emails(request.get("currentApproverEmail"))
        else:
            recipients = split_emails(request.get("currentApproverEmail"))
        recipients = [e for e in recipients if e and normalize_email(e) != excluded]
        out: list[OutgoingMail] = []
        for email in recipients:
            out += self._single(MailKind.REQUEST_UPDATED, request, email, "[貯蔵品購入申請] 内容更新 " + rid, body)
        return out

    # --- 完了 ---------------------------------------------------------------
    def completed(self, request: Mapping) -> list[OutgoingMail]:
        """sendCompletedEmail_。本文に PDF の URL（ドライブ）を入れる。"""
        rid = js_str(request.get("requestId"))
        lines = [
            js_str(request.get("applicantName")) + " 様",
            "",
            "貯蔵品購入申請が完了しました。",
            "",
            "申請番号: " + rid,
            "確定金額: " + amount_text(request),
            ("PDF: " + js_str(request.get("pdfUrl"))) if request.get("pdfUrl") else "",
            "",
            self.url(rid),
        ]
        body = "\n".join(line for line in lines if line)  # GAS: .filter(Boolean) で空行も落ちる
        return self._single(MailKind.COMPLETED, request, js_str(request.get("applicantEmail")), "[貯蔵品購入申請] 完了 " + rid, body)

    # --- 総務部への通知 -----------------------------------------------------
    def general_affairs_body(self, request: Mapping, heading: str) -> str:
        """buildGeneralAffairsBody_。"""
        rid = js_str(request.get("requestId"))
        return "\n".join([
            salutation_for_step(self.masters, request, STEPS.GENERAL_MANAGER, None),
            "",
            heading,
            "",
            "申請番号: " + rid,
            "申請者: " + js_str(request.get("applicantName")) + "（" + js_str(request.get("applicantEmail")) + "）",
            "部署: " + js_str(request.get("department")),
            "確定金額: " + amount_text(request),
            "現在ステップ: " + current_step_label(request),
            "",
            self.url(rid),
        ])

    def _typed_notification(self, kind: str, request: Mapping, type_: str, fallback_email: str, subject: str,
                            body: str, attach_pdf: bool) -> list[OutgoingMail]:
        """sendTypedNotification_: 種別の To / Cc。空なら fallback を To。Cc だけなら To へ上げる。"""
        if not self.enabled:
            return []
        to, cc = recipients_split(self.masters, type_)
        if not to and not cc:
            if fallback_email:
                to = [fallback_email]
            else:
                return []
        if not to:
            to, cc = cc, []
        # fallback は「a,b」の 1 文字列で入るので、メールごとに分ける（ヘッダは同じになる）。
        to = [e for t in to for e in (split_emails(t) or [t])]
        return [OutgoingMail(kind=kind, to=to, cc=cc, subject=subject, body=body, attach_pdf=attach_pdf,
                             request_id=js_str(request.get("requestId")))]

    def general_affairs(self, kind: str, request: Mapping, subject: str, body: str, attach_pdf: bool = False) -> list[OutgoingMail]:
        """notifyGeneralAffairs_。宛先が無ければ承認者マスタの総務部長へ。"""
        return self._typed_notification(kind, request, RECIPIENT_TYPES.GENERAL_AFFAIRS,
                                        resolve_general_manager_email(self.masters, request), subject, body, attach_pdf)

    # --- 購買（見積依頼・手配依頼） -----------------------------------------
    def _purchasing_by_category(self, kind: str, request: Mapping, subject: str, body: str) -> list[OutgoingMail]:
        """sendPurchasingByCategory_。To＝主担当の To、Cc＝主担当の Cc ＋ もう一方の購買の全員。"""
        if not self.enabled:
            return []
        primary, secondary = purchasing_types_for_category(request.get("category"))
        to, cc = recipients_split(self.masters, primary)
        if not to:
            fb = split_emails(_purchasing_fallback_by_type(self.masters, primary, request))
            if fb:
                to = fb
            elif cc:
                to, cc = cc, []
        secondary_emails = recipient_emails(self.masters, secondary)
        if not secondary_emails:
            secondary_emails = split_emails(_purchasing_fallback_by_type(self.masters, secondary, request))
        seen = set(to)
        merged_cc: list[str] = []
        for email in cc + secondary_emails:
            if email and email not in seen:
                seen.add(email)
                merged_cc.append(email)
        if not to and not merged_cc:
            return []
        if not to:
            to, merged_cc = merged_cc, []
        return [OutgoingMail(kind=kind, to=to, cc=merged_cc, subject=subject, body=body, attach_pdf=True,
                             request_id=js_str(request.get("requestId")))]

    def _purchasing_body(self, request: Mapping, lead: list[str]) -> str:
        rid = js_str(request.get("requestId"))
        return "\n".join([purchasing_salutation(self.masters, request), ""] + lead + [
            "",
            "申請番号: " + rid,
            "申請者: " + js_str(request.get("applicantName")) + "（" + js_str(request.get("applicantEmail")) + "）",
            "部署: " + js_str(request.get("department")),
            "品目区分: " + category_label(request),
            "確定金額: " + amount_text(request),
            "",
            self.url(rid),
        ])

    def quote_request(self, request: Mapping) -> list[OutgoingMail]:
        rid = js_str(request.get("requestId"))
        body = self._purchasing_body(request, [
            "見積をお願いします。金額確定後、アプリ内で各明細の単価をご入力ください。",
            "押印済の申請書PDFを添付します。",
        ])
        return self._purchasing_by_category(MailKind.QUOTE_REQUEST, request, "[貯蔵品購入申請] 見積依頼 " + rid, body)

    def purchasing_request(self, request: Mapping) -> list[OutgoingMail]:
        rid = js_str(request.get("requestId"))
        body = self._purchasing_body(request, ["押印済の申請書PDFを添付します。手配をお願いします。"])
        return self._purchasing_by_category(MailKind.PURCHASING_REQUEST, request, "[貯蔵品購入申請] 手配依頼 " + rid, body)

    # --- 見積確定・至急（confirmQuote / confirmExpedited の送信部分） ------
    def quote_confirmed(self, request: Mapping, gm: Mapping, amount_summary: str, total: Any, threshold: Any,
                        over: bool) -> list[OutgoingMail]:
        """総務部への金額確定の通知 ＋ 総務部長（代表 1 人）への承認依頼。"""
        rid = js_str(request.get("requestId"))
        over_banner = "\n".join([
            "━━━━━━━━━━━━━━━━━━━━━━",
            "🚨🚨🚨  要・社長決裁  🚨🚨🚨",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "確定金額（税抜）" + format_currency(total) + " は社長決裁しきい値（税抜 " + format_currency(threshold) + "）以上です。",
            "総務部長の承認後、必ず【社長の承認】が必要です。至急ご対応ください。",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ])
        if over:
            subject = "【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 " + rid + "（税抜10万円以上）"
            heading = over_banner + "\n\n購買が見積金額 " + amount_summary + " を確定しました。"
        else:
            subject = "[貯蔵品購入申請] 金額確定 " + rid
            heading = "購買が見積金額 " + amount_summary + " を確定しました。総務部長承認をお願いします。"
        kind = MailKind.GA_QUOTE_CONFIRMED_OVER if over else MailKind.GA_QUOTE_CONFIRMED
        return (self.general_affairs(kind, request, subject, self.general_affairs_body(request, heading))
                + self.approval_request(request, gm, over_banner if over else ""))

    def expedited(self, request: Mapping, gm: Mapping, comment: str) -> list[OutgoingMail]:
        """総務部への至急承認の通知 ＋ 総務部長（代表 1 人）への至急の承認依頼。"""
        rid = js_str(request.get("requestId"))
        banner = "\n".join([
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⚡⚡⚡ 至急承認のお願い（金額確定不要）⚡⚡⚡",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "この申請は金額確定が不要な標準品（価格は商社と確定済み）です。",
            "生産を止めないため、購買にて至急手配を開始したい案件です。",
            "金額確定は行いません。総務部長の至急承認をお願いします。",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ])
        clean = sanitize_text(comment, 1000)
        heading = banner + "\n\n購買より、金額確定不要・至急手配の申請です。" + ("\n備考: " + clean if clean else "")
        return (self.general_affairs(MailKind.GA_EXPEDITED, request, "【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] " + rid,
                                     self.general_affairs_body(request, heading))
                + self.approval_request(request, gm, banner, "【⚡至急承認・金額確定不要⚡】"))

    # --- 手配完了（completeAndNotify_ の送信部分。PDF 作成の後に呼ぶ） ------
    def arranged_and_completed(self, request: Mapping) -> list[OutgoingMail]:
        rid = js_str(request.get("requestId"))
        body = self.general_affairs_body(request, "貯蔵品購入申請の手配が完了しました。")
        return (self.general_affairs(MailKind.GA_ARRANGED, request, "[貯蔵品購入申請] 手配完了 " + rid, body, attach_pdf=True)
                + self.completed(request))


def job_failure_admin_mail(settings: Mapping[str, Any], fn: str, request_id: str, message: str,
                           extra_admins: Sequence[str] = ()) -> list[OutgoingMail]:
    """notifyAdminsOfJobFailure_。通知の停止設定を見ずに送る（GAS 版と同じ）。

    VPS 版は自動で送り直すので、1 行目の「（自動での再送はしません）」を「送り直しても失敗しました」に、
    最後の行を管理画面の案内に変えている（決定事項 9-2 で動きが変わったため）。
    """
    admins = []
    for e in split_emails(settings.get("adminEmails") or "") + [normalize_email(x) for x in extra_admins]:
        if e and e not in admins:
            admins.append(e)
    if not admins:
        return []
    body = "\n".join([
        "承認などの後に行う通知メール・PDF作成が失敗しました（時間をおいて送り直しましたが、成功しませんでした）。",
        "処理: " + fn,
        "申請: " + (request_id or "（不明）"),
        "エラー: " + message,
        "",
        "申請の詳細画面から必要に応じて「PDF再作成」を行い、関係者へ連絡してください。",
        "失敗の一覧は管理画面の「失敗した後処理」で確認できます。",
    ])
    return [OutgoingMail(kind=MailKind.ADMIN_JOB_FAILURE, to=admins, subject="[" + APP_NAME + "] 通知・PDFの後処理に失敗しました " + request_id,
                         body=body, request_id=request_id)]
