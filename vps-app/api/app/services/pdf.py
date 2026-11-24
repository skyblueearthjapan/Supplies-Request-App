"""申請書 PDF（GAS 版 Pdf.gs + PdfTemplate.html の移植）。

流れ: build_pdf_view()（押印・明細・履歴を決める。DB に依存しない）
      → render_pdf_html()（Jinja2・自動エスケープ）
      → PdfRenderer.render()（Playwright の Chromium で A4 の PDF。必ず 1 ページ）

GAS 版からの変更（計画書 9-2 の決定）:
- 押印は氏名。押した人のメールから上席マスタ（上席）・承認者マスタ（総務部長・社長・購買）の氏名を引く。
  カッコ書きは除く。マスタに無ければ履歴の操作者名。申請者の印は申請者氏名。
- まだ来ていない段の印は空欄（画面のプレビューと同じ。リコール後・再提出後に前の回の印を出さない）。
- 必ず 1 ページ（明細・履歴が多いときは詰めた組み方にし、履歴は新しい方から HISTORY_MAX 件。
  それでもあふれたら Chromium で縮小して収める）。
- バーコードは QR（詳細画面の URL）。日付は日本時間。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from .mailpdf_common import (
    ACTION,
    ACTION_LABELS,
    CATEGORY_LABELS,
    REASONS,
    STATUS,
    STATUS_LABELS,
    STEP_LABELS,
    STEPS,
    Masters,
    date_only,
    format_currency,
    format_datetime,
    gross_amount,
    js_str,
    normalize_category,
    normalize_email,
    parse_boolean,
    parse_number,
    request_url,
    route_of,
    sanitize_file_name,
    sanitize_text,
    split_emails,
    strip_role_paren,
    tax_amount,
)
from .recipients import find_approver_rule, find_star_rule, resolve_supervisors

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
HISTORY_MAX = 20  # 1 ページに収めるため、承認履歴は新しい方からこの件数まで
STAMP_ACTIONS = (ACTION.APPROVE, ACTION.COMPLETE, ACTION.RETURN, ACTION.QUOTE, ACTION.EXPEDITE)

# A4 の高さ（CSS px, 96dpi）。297mm = 1122.5px
A4_HEIGHT_PX = 297 / 25.4 * 96


def pdf_file_name(request: Mapping[str, Any]) -> str:
    """GAS 版と同じ: <申請ID>_<申請者名>_貯蔵品購入申請書.pdf"""
    return js_str(request.get("requestId")) + "_" + sanitize_file_name(request.get("applicantName")) + "_貯蔵品購入申請書.pdf"


def era_date(value: Any) -> str:
    """データネーム印の中央帯（元号年.月.日）。例: 2026-05-27 → 令8. 5. 27。日時は日本時間の日付。"""
    s = date_only(value)
    parts = s.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts) or len(parts[0]) != 4:
        return ""
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    n = y - 2018
    era = f"令{n}" if n >= 1 else str(y % 100)
    return f"{era}. {m}. {d}"


def name_font(name: Any) -> str:
    n = len("".join(js_str(name).split()))
    if n <= 2:
        return "9pt"
    if n == 3:
        return "7.6pt"
    return "6.4pt"


# ---------------------------------------------------------------------------
# 押印の氏名
# ---------------------------------------------------------------------------

def _rule_candidates(masters: Masters, request: Mapping) -> list[dict]:
    """この申請の承認者マスタ行 → '*' 行 → そのほかの有効な行の順。"""
    out: list[dict] = []
    first = find_approver_rule(masters, request.get("applicantEmail"), request.get("department"))
    for r in [first, find_star_rule(masters)] + [r for r in masters.approver_rules if r["active"]]:
        if r is not None and all(r is not x for x in out):
            out.append(r)
    return out


def _name_from_rules(masters: Masters, request: Mapping, email: str, fields: Sequence[str]) -> str:
    for rule in _rule_candidates(masters, request):
        for fld in fields:
            if email in split_emails(rule.get(fld + "Email")) and rule.get(fld + "Name"):
                return rule[fld + "Name"]
    return ""


def stamp_master_name(masters: Masters, request: Mapping, step: str, actor_email: Any) -> str:
    """押した人のメールからマスタの氏名を引く（無ければ ''）。カッコ書きは除く。"""
    email = normalize_email(actor_email)
    if not email:
        return ""
    name = ""
    if step == STEPS.SUPERVISOR:
        # 上席マスタ: 申請の部署の行 → '*' の行 → ほかの部署の行（同じ人が複数部署の上席のことがある）
        rows = [r for r in masters.dept_supervisors if r["email"] == email and r["name"]]
        dept = sanitize_text(request.get("department"), 100)
        pick = (next((r for r in rows if r["department"] == dept), None)
                or next((r for r in rows if r["department"] == "*"), None)
                or (rows[0] if rows else None))
        name = pick["name"] if pick else ""
        if not name:
            name = _name_from_rules(masters, request, email, ["supervisor"])
    elif step == STEPS.GENERAL_MANAGER:
        name = _name_from_rules(masters, request, email, ["generalManager"])
    elif step == STEPS.PRESIDENT:
        name = _name_from_rules(masters, request, email, ["president"])
    elif step in (STEPS.PURCHASING, STEPS.PURCHASING_QUOTE):
        name = _name_from_rules(masters, request, email, ["purchasing", "purchasingElec"])
    return strip_role_paren(name)


def supervisor_title_by_email(masters: Masters, request: Mapping, email: Any) -> str:
    target = normalize_email(email)
    for s in resolve_supervisors(masters, request.get("applicantEmail"), request.get("department")):
        if s["email"] == target:
            return s.get("title") or ""
    return ""


def resolve_step_titles(masters: Masters, request: Mapping, history: Sequence[Mapping]) -> dict[str, str]:
    """resolveStepTitles_: 印の下に出す役職。空なら段の名前を出す（購買見積は常に段の名前）。"""
    titles: dict[str, str] = {}
    rule = find_approver_rule(masters, request.get("applicantEmail"), request.get("department"))
    if rule:
        titles[STEPS.GENERAL_MANAGER] = rule.get("generalManagerTitle") or ""
        titles[STEPS.PRESIDENT] = rule.get("presidentTitle") or ""
        titles[STEPS.PURCHASING] = rule.get("purchasingTitle") or ""
    sup_email = ""
    for h in reversed(history):
        if h.get("fromStep") == STEPS.SUPERVISOR and h.get("action") in (ACTION.APPROVE, ACTION.RETURN):
            sup_email = js_str(h.get("actorEmail"))
            break
    if not sup_email and request.get("currentStep") == STEPS.SUPERVISOR:
        sup_email = js_str(request.get("currentApproverEmail"))
    sup_title = supervisor_title_by_email(masters, request, sup_email) if sup_email else ""
    titles[STEPS.SUPERVISOR] = sup_title or ((rule.get("supervisorTitle") or "") if rule else "")
    return titles


# ---------------------------------------------------------------------------
# 押印の選び方
# ---------------------------------------------------------------------------

@dataclass
class Stamp:
    step: str
    role_label: str
    acted: bool
    name: str = ""
    date: str = ""
    title: str = ""
    ng: bool = False

    @property
    def name_font(self) -> str:
        return name_font(self.name)


def stamp_for(step: str, request: Mapping, history: Sequence[Mapping], masters: Masters,
              titles: Mapping[str, str]) -> Stamp:
    """1 段の印。プレビュー（Client.html の stampFor）と同じ決まりで、押す・空欄を決める。"""
    role_label = STEP_LABELS.get(step) or step
    empty = Stamp(step=step, role_label=role_label, acted=False)
    if step == STEPS.APPLICANT:
        for h in reversed(history):
            if h.get("action") in (ACTION.SUBMIT, ACTION.RESUBMIT):
                name = js_str(request.get("applicantName")) or js_str(h.get("actorName")) or js_str(h.get("actorEmail"))
                return Stamp(step=step, role_label=role_label, acted=True, name=strip_role_paren(name) or name,
                             date=era_date(h.get("happenedAt")), title=titles.get(step) or role_label)
        return empty
    # まだ来ていない段（現在の段とそれより後）は、完了の申請を除いて空欄。
    route = route_of(request)
    ki = route.index(step) if step in route else -1
    ci = route.index(request.get("currentStep")) if request.get("currentStep") in route else -1
    if ki >= 0 and ci >= 0 and ki >= ci and request.get("status") != STATUS.COMPLETED:
        return empty
    for h in reversed(history):
        if h.get("fromStep") == step and h.get("action") in STAMP_ACTIONS:
            name = stamp_master_name(masters, request, step, h.get("actorEmail")) \
                or js_str(h.get("actorName")) or js_str(h.get("actorEmail"))
            return Stamp(step=step, role_label=role_label, acted=True, name=name, date=era_date(h.get("happenedAt")),
                         title=titles.get(step) or role_label, ng=h.get("action") == ACTION.RETURN)
    return empty


def select_stamps(request: Mapping, history: Sequence[Mapping], masters: Masters) -> list[Stamp]:
    titles = resolve_step_titles(masters, request, history)
    return [stamp_for(step, request, history, masters, titles) for step in [STEPS.APPLICANT] + route_of(request)]


# ---------------------------------------------------------------------------
# 1 ページに収める組み方
# ---------------------------------------------------------------------------

def density_level(item_count: int, history_rows: Sequence[Mapping]) -> int:
    """0: ふつう / 1: 詰める / 2: もっと詰める。明細は 2 段組なので重く数える。"""
    load = item_count * 1.8 + len(history_rows)
    load += sum(len(js_str(h.get("comment"))) // 45 for h in history_rows)
    if load <= 26:
        return 0
    if load <= 40:
        return 1
    return 2


def _qr_svg(url: str) -> Markup:
    import segno

    qr = segno.make_qr(url, error="m")
    return Markup(qr.svg_inline(scale=2, border=0, dark="#0e0e0e"))  # 中身は segno が作る SVG（利用者の入力は URL のみ）


def build_pdf_view(request: Mapping[str, Any], items: Sequence[Mapping[str, Any]], history: Sequence[Mapping[str, Any]],
                   masters: Masters, public_origin: str, threshold: Any = None) -> dict[str, Any]:
    """テンプレートに渡す値を作る。request はシートの列名（camelCase）の dict。"""
    hist = sorted(history, key=lambda h: format_datetime(h.get("happenedAt")))  # 安定ソート（同じ秒は渡された順）
    items_sorted = sorted(items, key=lambda it: parse_number(it.get("lineNo")))
    omitted = max(0, len(hist) - HISTORY_MAX)
    shown = hist[omitted:]
    route = route_of(request)
    total = parse_number(request.get("totalAmount"))
    waived = parse_boolean(request.get("amountWaived"))
    status = js_str(request.get("status"))
    rid = js_str(request.get("requestId"))
    url = request_url(public_origin, rid)

    def money(v: Any) -> str:
        return format_currency(v) if parse_number(v) > 0 else "未定"

    return {
        "requestId": rid,
        "requestDate": date_only(request.get("requestDate")),
        "over": STEPS.PRESIDENT in route,
        "watermark": "完了" if status == STATUS.COMPLETED else ("差戻" if status == STATUS.RETURNED else ""),
        "stamps": select_stamps(request, hist, masters),
        "department": js_str(request.get("department")),
        "applicantName": js_str(request.get("applicantName")),
        "applicantEmail": js_str(request.get("applicantEmail")),
        "categoryLabel": CATEGORY_LABELS[normalize_category(request.get("category"))],
        "statusLabel": STATUS_LABELS.get(status) or status,
        "itemCount": len(items_sorted),
        "items": [
            {
                "no": i + 1,
                "name": js_str(it.get("name")),
                "sub": "・".join(x for x in (js_str(it.get("model")), js_str(it.get("maker"))) if x) or "—",
                "quantity": js_str(it.get("quantity")),
                "unit": js_str(it.get("unit")),
                "unitPrice": money(it.get("unitPrice")),
                "amount": money(it.get("amount")),
                "desiredDeliveryDate": date_only(it.get("desiredDeliveryDate")) or "—",
                "note": js_str(it.get("note")) or "—",
            }
            for i, it in enumerate(items_sorted)
        ],
        "hasTotal": total > 0,
        "subtotal": format_currency(total),
        "tax": format_currency(tax_amount(total)),
        "gross": format_currency(gross_amount(total)),
        "totalText": format_currency(gross_amount(total)) if total > 0 else ("金額確定不要（至急）" if waived else "未確定"),
        "reasons": [{"code": r["code"], "label": r["label"], "on": request.get("reasonCode") == r["code"]} for r in REASONS],
        "reasonDetail": js_str(request.get("reasonDetail")) or "—",
        "historyCount": len(hist),
        "historyOmitted": omitted,
        "history": [
            {
                "happenedAt": format_datetime(h.get("happenedAt")),
                "action": ACTION_LABELS.get(h.get("action")) or js_str(h.get("action")),
                "actor": js_str(h.get("actorName")) or js_str(h.get("actorEmail")),
                "fromStep": STEP_LABELS.get(h.get("fromStep")) or js_str(h.get("fromStep")),
                "toStep": STEP_LABELS.get(h.get("toStep")) or js_str(h.get("toStep")),
                "comment": js_str(h.get("comment")) or "—",
            }
            for h in shown
        ],
        "density": density_level(len(items_sorted), shown),
        "detailUrl": url,
        "qrSvg": _qr_svg(url),
    }


_env: Environment | None = None


def _environment() -> Environment:
    global _env
    if _env is None:
        _env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                           autoescape=select_autoescape(["html", "j2", "html.j2"], default=True),
                           trim_blocks=True, lstrip_blocks=True)
    return _env


def render_pdf_html(view: Mapping[str, Any]) -> str:
    return _environment().get_template("pdf.html.j2").render(v=view)


# ---------------------------------------------------------------------------
# Chromium で PDF にする
# ---------------------------------------------------------------------------

# はみ出すときは縮める。zoom だけだと幅も縮んで折り返しが増えるので、幅を 1/zoom に広げて
# 見た目の幅を A4 のままにし、収まるまで数回くり返す。
_FIT_SCRIPT = """
(pageHeight) => {
  const doc = document.querySelector('.pdf-doc');
  let z = 1;
  for (let i = 0; i < 8; i++) {
    const h = doc.getBoundingClientRect().height;
    if (h <= pageHeight) { return z; }
    z = z * (pageHeight / h) * 0.99;
    doc.style.zoom = String(z);
    doc.style.width = (210 / z) + 'mm';
    doc.style.minHeight = '0';
  }
  return z;
}
"""


class PdfRenderer:
    """Playwright の Chromium を 1 つ開いたまま使い回す（worker の中で 1 つ）。"""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None

    def _ensure(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        return self._browser

    def render(self, html: str) -> bytes:
        browser = self._ensure()
        page = browser.new_page(viewport={"width": 794, "height": 1123})
        try:
            page.emulate_media(media="print")
            page.set_content(html, wait_until="load")
            page.evaluate("document.fonts.ready.then(() => true)")
            # 少しでもはみ出すなら縮めて 1 ページに収める（はみ出した 2 ページ目を作らない）。
            page.evaluate(_FIT_SCRIPT, A4_HEIGHT_PX - 2)
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        finally:
            page.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None
