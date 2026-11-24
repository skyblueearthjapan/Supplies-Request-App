"""承認者マスタ・部署別上席マスタ・通知宛先の読み方（Workflow.gs 後半・DeptSupervisors.gs・Recipients.gs）。

Masters.load(db) で 1 回の操作の間のマスタを読み込み、探し方はすべてこのクラスのメソッドで行う。
行の順番（sort_order）に意味がある（最初に当たった行を使う・先頭が代表）。
担当 M（メールの宛先・PDF の押印）からも同じメソッドを使えるようにしてある。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import ACTION, CATEGORIES, STATUS, STEPS
from app.domain.helpers import (
    normalize_category,
    normalize_email,
    normalize_email_list,
    parse_boolean,
    sanitize_text,
    strip_role_paren,
)
from app.domain.labels import STEP_LABELS
from app.models import ApproverRule, DeptSupervisor, NotificationRecipient
from app.rpc import RpcError
from app.serialize import approver_row, dept_supervisor_row, recipient_row

ERR_NO_SUPERVISOR = "上席承認者が未設定です。部署別 上席マスタ（または承認者マスタの上席）を設定してください。"
ERR_NO_APPROVER_RULE = "承認者マスタが未設定です。管理画面で部署または申請者の承認者を登録してください。"

# 押印に使う履歴の操作（画面のプレビュー・PDF で共用）
STAMP_ACTIONS = (ACTION.APPROVE, ACTION.COMPLETE, ACTION.RETURN, ACTION.QUOTE, ACTION.EXPEDITE)


def _js(value: Any) -> str:
    return "" if value is None else str(value)


def pick_stamp_history(step: str, route: Sequence[str], current_step: Any, status: Any,
                       history: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """その段の印に使う履歴（無ければ None）。history は古い順。画面の stampFor・PDF と同じ決まり。

    申請者: 最後の申請・再申請。そのほか: まだ来ていない段（現在の段とそれより後）は完了の申請を除いて空欄、
    それ以外は その段から出た最後の承認・完了処理・差戻・金額確定・金額不要至急。
    """
    if step == STEPS.APPLICANT:
        return next((h for h in reversed(history) if h.get("action") in (ACTION.SUBMIT, ACTION.RESUBMIT)), None)
    route = list(route or [])
    ki = route.index(step) if step in route else -1
    ci = route.index(current_step) if current_step in route else -1
    if ki >= 0 and ci >= 0 and ki >= ci and status != STATUS.COMPLETED:
        return None
    return next((h for h in reversed(history) if h.get("fromStep") == step and h.get("action") in STAMP_ACTIONS), None)


def to_client_approver(row: dict[str, Any]) -> dict[str, Any]:
    """toClientApprover_。row は APPROVER_COLUMNS の行（画面から来た値もこれで整える）。"""
    row = row if isinstance(row, dict) else {}
    active = row.get("active")
    return {
        "department": sanitize_text(row.get("department"), 100),
        "applicantEmail": normalize_email(row.get("applicantEmail")),
        "applicantName": sanitize_text(row.get("applicantName"), 100),
        "supervisorEmail": normalize_email(row.get("supervisorEmail")),
        "supervisorName": sanitize_text(row.get("supervisorName"), 100),
        "generalManagerEmail": normalize_email_list(row.get("generalManagerEmail")),
        "generalManagerName": sanitize_text(row.get("generalManagerName"), 100),
        "presidentEmail": normalize_email_list(row.get("presidentEmail")),
        "presidentName": sanitize_text(row.get("presidentName"), 100),
        "purchasingEmail": normalize_email_list(row.get("purchasingEmail")),
        "purchasingName": sanitize_text(row.get("purchasingName"), 100),
        "purchasingElecEmail": normalize_email_list(row.get("purchasingElecEmail")),
        "purchasingElecName": sanitize_text(row.get("purchasingElecName"), 100),
        # JS: row.active === '' ? true : parseBoolean_(row.active)（undefined は false）
        "active": True if active == "" else parse_boolean(active),
        "supervisorTitle": sanitize_text(row.get("supervisorTitle"), 40),
        "generalManagerTitle": sanitize_text(row.get("generalManagerTitle"), 40),
        "presidentTitle": sanitize_text(row.get("presidentTitle"), 40),
        "purchasingTitle": sanitize_text(row.get("purchasingTitle"), 40),
        "purchasingElecTitle": sanitize_text(row.get("purchasingElecTitle"), 40),
    }


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


def approver_members_from_field(rule: dict[str, Any], field: str, default_name: str) -> list[dict[str, str]]:
    from app.domain.helpers import split_emails

    name = rule.get(field + "Name") or default_name or ""
    title = rule.get(field + "Title") or ""
    return [{"email": email, "name": name, "title": title} for email in split_emails(rule.get(field + "Email"))]


@dataclass
class Masters:
    approvers: list[dict[str, Any]]        # toClientApprover_ 済み（行の順）
    dept_supervisors: list[dict[str, Any]]  # getDeptSupervisors_ の形（行の順）
    recipients: list[dict[str, Any]]        # getRecipients_ の形（行の順）

    @classmethod
    def load(cls, db: Session) -> "Masters":
        approvers = [
            to_client_approver(approver_row(a))
            for a in db.execute(select(ApproverRule).order_by(ApproverRule.sort_order, ApproverRule.id)).scalars()
        ]
        sups = []
        for d in db.execute(select(DeptSupervisor).order_by(DeptSupervisor.sort_order, DeptSupervisor.id)).scalars():
            row = dept_supervisor_row(d)
            sups.append({
                "department": sanitize_text(row["department"], 100),
                "email": normalize_email(row["email"]),
                "name": sanitize_text(row["name"], 100),
                "title": sanitize_text(row["title"], 40),
                "active": bool(row["active"]),
                "notifyEmail": normalize_email(row["notifyEmail"]),
            })
        recipients = []
        for r in db.execute(
            select(NotificationRecipient).order_by(NotificationRecipient.sort_order, NotificationRecipient.id)
        ).scalars():
            row = recipient_row(r)
            if not row["email"]:
                continue
            recipients.append({
                "type": sanitize_text(row["type"], 50),
                "email": normalize_email(row["email"]),
                "name": sanitize_text(row["name"], 100),
                "active": bool(row["active"]),
                "sendAs": "CC" if str(row["sendAs"]).upper() == "CC" else "TO",
            })
        return cls(approvers=approvers, dept_supervisors=sups, recipients=recipients)

    # ===== 承認者マスタ =====

    def active_approvers(self) -> list[dict[str, Any]]:
        return [row for row in self.approvers if row["active"]]

    def find_approver_rule(self, applicant_email: Any, department: Any) -> dict[str, Any] | None:
        """申請者メールの一致 → 部署の一致 → '*' の順に、最初に当たった有効な行。"""
        email = normalize_email(applicant_email)
        dept = sanitize_text(department, 100)
        rows = self.active_approvers()
        for row in rows:
            if row["applicantEmail"] and row["applicantEmail"] == email:
                return row
        for row in rows:
            if dept and row["department"] == dept and not row["applicantEmail"]:
                return row
        for row in rows:
            if row["department"] == "*" and not row["applicantEmail"]:
                return row
        return None

    def require_approver_rule(self, applicant_email: Any, department: Any) -> dict[str, Any]:
        rule = self.find_approver_rule(applicant_email, department)
        if rule is None:
            raise RpcError(ERR_NO_APPROVER_RULE)
        return rule

    def find_star_rule(self) -> dict[str, Any] | None:
        for row in self.active_approvers():
            if row["department"] == "*" and not row["applicantEmail"]:
                return row
        return None

    def resolve_step_approvers(self, rule: dict[str, Any] | None, step: str, category: Any = None) -> list[dict[str, str]]:
        """役職ステップの承認者集合。購買はメカ・電気の両方（代表は品目区分で決める）。"""
        if not rule:
            return []
        field = step_role_field(step)
        if not field:
            return []
        if field == "purchasing":
            mech = approver_members_from_field(rule, "purchasing", step_role_default_name(step))
            elec = approver_members_from_field(rule, "purchasingElec", "電気購買")
            ordered = elec + mech if normalize_category(category) == CATEGORIES.ELEC else mech + elec
            seen: set[str] = set()
            members = []
            for member in ordered:
                if member["email"] and member["email"] not in seen:
                    seen.add(member["email"])
                    members.append(member)
            return members
        return approver_members_from_field(rule, field, step_role_default_name(step))

    def resolve_step_approvers_with_star(self, rule: dict[str, Any] | None, step: str, category: Any = None):
        approvers = self.resolve_step_approvers(rule, step, category)
        if approvers:
            return approvers
        if not rule or rule["department"] != "*":
            star = self.find_star_rule()
            if star:
                return self.resolve_step_approvers(star, step, category)
        return approvers

    def get_approver_for_step(self, rule: dict[str, Any], step: str, category: Any = None) -> dict[str, str]:
        if step == STEPS.SUPERVISOR:
            supervisor = {"email": normalize_email(rule.get("supervisorEmail")), "name": rule.get("supervisorName") or "上席"}
            if not supervisor["email"]:
                raise RpcError(STEP_LABELS[step] + "の承認者メールアドレスが未設定です。")
            return supervisor
        approvers = self.resolve_step_approvers_with_star(rule, step, category)
        if not approvers or not approvers[0]["email"]:
            raise RpcError(STEP_LABELS.get(step, step) + "の承認者メールアドレスが未設定です。")
        first = approvers[0]
        return {"email": first["email"], "name": first["name"], "title": first["title"]}

    def is_step_approver_for(self, email: Any, applicant_email: Any, department: Any, step: str) -> bool:
        if step == STEPS.SUPERVISOR:
            return self.is_supervisor_for(email, applicant_email, department)
        if not step_role_field(step):
            return False
        target = normalize_email(email)
        if not target:
            return False
        rule = self.find_approver_rule(applicant_email, department)
        return any(a["email"] == target for a in self.resolve_step_approvers_with_star(rule, step))

    def is_general_manager_anywhere(self, email: Any) -> bool:
        """承認者マスタのどこかで総務部長に登録されているか（上席承認待ちを全部署で見せる判定）。"""
        from app.domain.helpers import split_emails

        target = normalize_email(email)
        if not target:
            return False
        return any(target in split_emails(row["generalManagerEmail"]) for row in self.active_approvers())

    def role_name_title(self, applicant_email: Any, department: Any, step: str) -> dict[str, str]:
        """roleNameTitle_（Email.gs）: 役職ステップの氏名・役職（部署別 → '*'、既定名は使わない）。"""
        field = step_role_field(step)
        if not field:
            return {"name": "", "title": ""}
        rule = self.find_approver_rule(applicant_email, department)
        name = rule.get(field + "Name", "") if rule else ""
        title = rule.get(field + "Title", "") if rule else ""
        if not name:
            star = self.find_star_rule()
            if star:
                name = star.get(field + "Name") or name
                if not title:
                    title = star.get(field + "Title") or ""
        return {"name": name, "title": title}

    def purchasing_stamp_name(self, applicant_email: Any, department: Any) -> str:
        return strip_role_paren(self.role_name_title(applicant_email, department, STEPS.PURCHASING)["name"])

    # ===== 押印の氏名（画面の詳細 stampNames と PDF で共用） =====

    def _rule_candidates(self, applicant_email: Any, department: Any) -> list[dict[str, Any]]:
        """この申請の承認者マスタ行 → '*' 行 → そのほかの有効な行の順。"""
        out: list[dict[str, Any]] = []
        for r in [self.find_approver_rule(applicant_email, department), self.find_star_rule()] + self.active_approvers():
            if r is not None and all(r is not x for x in out):
                out.append(r)
        return out

    def _first_email_name(self, email: str, applicant_email: Any, department: Any, field: str) -> str:
        """欄の先頭のメールが email の行の氏名。2 人目以降のメールには使わない（氏名は 1 人分しか無いため）。"""
        from app.domain.helpers import split_emails

        for rule in self._rule_candidates(applicant_email, department):
            emails = split_emails(rule.get(field + "Email"))
            if emails and emails[0] == email and rule.get(field + "Name"):
                return rule[field + "Name"]
        return ""

    def _dept_supervisor_name(self, email: str, department: Any) -> str:
        """上席マスタの氏名: 申請の部署の行 → '*' の行 → ほかの部署の行（同じ人が複数部署の上席のことがある）。"""
        rows = [r for r in self.dept_supervisors if r["email"] == email and r["name"]]
        dept = sanitize_text(department, 100)
        pick = (next((r for r in rows if r["department"] == dept), None)
                or next((r for r in rows if r["department"] == "*"), None)
                or (rows[0] if rows else None))
        return pick["name"] if pick else ""

    def stamp_name(self, step: str, request: Mapping[str, Any], actor_email: Any, actor_name: Any) -> str:
        """押印の氏名（決定事項 Q3）。request は applicantEmail・department・applicantName を持つ dict。

        - 申請者: 申請者氏名（無ければ履歴の操作者名）
        - 購買見積・購買手配: 承認者マスタの購買氏名（メカ購買。GAS 版と同じで、電気の案件でも変えない）
        - 上席: 上席マスタの氏名 → 承認者マスタの上席氏名
        - 総務部長・社長: 承認者マスタの氏名（欄の先頭のメールの人だけ）→ 上席マスタの氏名
        - どれも無ければ履歴の操作者名（無ければメール）。マスタの氏名のカッコ書きは除く。
        """
        applicant_email = request.get("applicantEmail")
        department = request.get("department")
        fallback = _js(actor_name) or _js(actor_email)
        if step == STEPS.APPLICANT:
            name = _js(request.get("applicantName")) or fallback
            return strip_role_paren(name) or name
        if step in (STEPS.PURCHASING, STEPS.PURCHASING_QUOTE):
            return self.purchasing_stamp_name(applicant_email, department) or fallback
        email = normalize_email(actor_email)
        name = ""
        if email and step == STEPS.SUPERVISOR:
            name = (self._dept_supervisor_name(email, department)
                    or self._first_email_name(email, applicant_email, department, "supervisor"))
        elif email and step in (STEPS.GENERAL_MANAGER, STEPS.PRESIDENT):
            field = "generalManager" if step == STEPS.GENERAL_MANAGER else "president"
            name = (self._first_email_name(email, applicant_email, department, field)
                    or self._dept_supervisor_name(email, department))
        return strip_role_paren(name) or fallback

    def stamp_names(self, request: Mapping[str, Any], route: Sequence[str],
                    history: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        """段ごとの押印の氏名（印が出る段だけ）。画面の stampFor はこれを使う。"""
        names: dict[str, str] = {}
        for step in [STEPS.APPLICANT] + list(route or []):
            h = pick_stamp_history(step, route, request.get("currentStep"), request.get("status"), history)
            if h is not None:
                names[step] = self.stamp_name(step, request, h.get("actorEmail"), h.get("actorName"))
        return names

    # ===== 部署別上席マスタ =====

    def resolve_supervisors(self, applicant_email: Any, department: Any) -> list[dict[str, str]]:
        """上席集合: 上席マスタの部署一致 → '*' → 承認者マスタの上席（1 人）。先頭が代表。"""
        dept = sanitize_text(department, 100)
        active = [row for row in self.dept_supervisors if row["active"] and row["email"]]
        matched = [row for row in active if row["department"] == dept]
        if not matched:
            matched = [row for row in active if row["department"] == "*"]
        if matched:
            return [
                {"email": row["email"], "name": row["name"] or "上席", "title": row["title"] or "",
                 "notifyEmail": row["notifyEmail"] or ""}
                for row in matched
            ]
        rule = self.find_approver_rule(applicant_email, department)
        if rule and rule["supervisorEmail"]:
            return [{"email": normalize_email(rule["supervisorEmail"]), "name": rule["supervisorName"] or "上席",
                     "title": rule["supervisorTitle"] or "", "notifyEmail": ""}]
        return []

    def require_supervisors(self, applicant_email: Any, department: Any) -> list[dict[str, str]]:
        supervisors = self.resolve_supervisors(applicant_email, department)
        if not supervisors:
            raise RpcError(ERR_NO_SUPERVISOR)
        return supervisors

    def is_supervisor_for(self, email: Any, applicant_email: Any, department: Any) -> bool:
        target = normalize_email(email)
        return any(s["email"] == target for s in self.resolve_supervisors(applicant_email, department))

    def supervisor_title_by_email(self, email: Any, applicant_email: Any, department: Any) -> str:
        target = normalize_email(email)
        for s in self.resolve_supervisors(applicant_email, department):
            if s["email"] == target:
                return s["title"] or ""
        return ""

    # ===== 通知宛先 =====

    def recipient_emails(self, type_: str) -> list[str]:
        seen: list[str] = []
        for row in self.recipients:
            if row["active"] and row["type"] == type_ and row["email"] and row["email"] not in seen:
                seen.append(row["email"])
        return seen

    def recipients_split(self, type_: str) -> dict[str, list[str]]:
        to: list[str] = []
        cc: list[str] = []
        seen: set[str] = set()
        for row in self.recipients:
            if not (row["active"] and row["type"] == type_ and row["email"]):
                continue
            if row["email"] in seen:
                continue
            seen.add(row["email"])
            (cc if row["sendAs"] == "CC" else to).append(row["email"])
        return {"to": to, "cc": cc}
