"""PDF: 押印の選び方（氏名・未到達の段は空欄・リコール後・再提出後）、日本時間の日付、1 ページ。

HTML の段階までのテストはどこでも動く。Chromium で PDF にするテストは、Chromium が起動できないと飛ばす
（VPS では `docker compose run --rm api pytest -m chromium` で必ず回す）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from mailpdf_fixtures import ORIGIN, approver_rules, make_items, make_masters, make_request

from app.services.pdf import (
    HISTORY_MAX,
    build_pdf_view,
    era_date,
    pdf_file_name,
    render_pdf_html,
    select_stamps,
)

ROUTE4 = '["上席","購買見積","総務部長","購買手配"]'
ROUTE5 = '["上席","購買見積","総務部長","社長","購買手配"]'


def h(action, from_step, to_step, at, email, name, comment=""):
    return {"action": action, "fromStep": from_step, "toStep": to_step, "happenedAt": at, "actorEmail": email,
            "actorName": name, "comment": comment}


SUBMIT = h("申請", "", "上席", "2026-11-20 09:00:00", "applicant@lineworks-local.info", "申請 太郎")
SUP_OK = h("承認", "上席", "購買見積", datetime(2026, 11, 20, 15, 30, tzinfo=timezone.utc),  # 日本時間 11/21 0:30
           "sup1@lineworks-local.info", "sup1")
QUOTE = h("金額確定", "購買見積", "総務部長", "2026-11-22 10:00:00", "elec@lineworks-local.info", "elec")
GM_OK = h("承認", "総務部長", "社長", "2026-11-23 10:00:00", "gm2@lineworks-local.info", "gm2")
PRES_OK = h("承認", "社長", "購買手配", "2026-11-23 11:00:00", "pres@lineworks-local.info", "pres")
DONE = h("完了処理", "購買手配", "完了済", "2026-11-24 10:00:00", "mech@lineworks-local.info", "mech")


def stamps_by_step(request, history, masters=None):
    return {s.step: s for s in select_stamps(request, history, masters or make_masters())}


def test_stamp_names_come_from_masters_and_dates_are_jst():
    req = make_request(routeJson=ROUTE5, currentStep="購買手配", totalAmount=150000)
    st = stamps_by_step(req, [SUBMIT, SUP_OK, QUOTE, GM_OK, PRES_OK])
    assert st["申請者"].acted and st["申請者"].name == "申請 太郎" and st["申請者"].date == "令8. 11. 20"
    assert st["上席"].name == "上田 一郎"  # 上席マスタの氏名（履歴の sup1 ではない）
    assert st["上席"].date == "令8. 11. 21"  # 0 時 30 分（日本時間）の承認はその日
    assert st["上席"].title == "課長"
    assert st["購買見積"].name == "五十嵐"  # 押した人が電気購買でも、GAS 版どおりメカ購買の氏名（カッコ除き）
    assert st["購買見積"].title == "購買（見積）"  # 購買見積の段は段の名前
    assert st["総務部長"].name == "gm2"  # 欄の 2 人目のメール → マスタの氏名は使わず履歴の操作者名
    assert st["総務部長"].title == "総務部長"  # 部署の行の役職が空 → 段の名前（'*' は見ない。GAS と同じ）
    assert st["社長"].name == "社長 太郎"
    assert not st["購買手配"].acted  # 今の段は空欄


def test_first_email_of_field_gets_master_name():
    req = make_request(routeJson=ROUTE4, currentStep="購買手配", totalAmount=3600)
    gm1 = h("承認", "総務部長", "購買手配", "2026-11-23 10:00:00", "gm1@lineworks-local.info", "gm1")
    st = stamps_by_step(req, [SUBMIT, SUP_OK, QUOTE, gm1])
    assert st["総務部長"].name == "辻野 一郎"  # 欄の先頭のメール → マスタの氏名（カッコ書きは除く）


def test_second_email_falls_back_to_supervisor_master():
    # 総務部長の欄の 2 人目が上席マスタにいれば、その氏名
    rules = [dict(r) for r in approver_rules()]
    rules[0]["generalManagerEmail"] = "gm1@lineworks-local.info, sup1@lineworks-local.info"
    req = make_request(routeJson=ROUTE4, currentStep="購買手配", totalAmount=3600)
    gm = h("承認", "総務部長", "購買手配", "2026-11-23 10:00:00", "sup1@lineworks-local.info", "sup1")
    st = stamps_by_step(req, [SUBMIT, SUP_OK, QUOTE, gm], make_masters(approver_rules=rules))
    assert st["総務部長"].name == "上田 一郎"


def test_pdf_and_detail_use_the_same_stamp_names():
    from app.services.pdf import stamp_masters

    req = make_request(routeJson=ROUTE5, currentStep="購買手配", totalAmount=150000)
    hist = [SUBMIT, SUP_OK, QUOTE, GM_OK, PRES_OK]
    masters = make_masters()
    names = stamp_masters(masters).stamp_names(req, ["上席", "購買見積", "総務部長", "社長", "購買手配"], hist)
    assert names == {s.step: s.name for s in select_stamps(req, hist, masters) if s.acted}


def test_applicant_stamp_is_applicant_name():
    req = make_request(routeJson=ROUTE4, currentStep="上席", applicantName="申請 次郎（製造）")
    assert stamps_by_step(req, [SUBMIT])["申請者"].name == "申請 次郎"


def test_unknown_actor_falls_back_to_history_name():
    req = make_request(routeJson=ROUTE4, currentStep="購買見積")
    other = h("承認", "上席", "購買見積", "2026-11-21 09:00:00", "admin@lineworks-local.info", "代理 記録者")
    assert stamps_by_step(req, [SUBMIT, other])["上席"].name == "代理 記録者"


def test_steps_not_yet_reached_are_blank_after_recall():
    # 総務部長が承認 → 購買手配から総務部長へリコール。総務部長の前の回の印は出さない。
    gm_to_purchasing = h("承認", "総務部長", "購買手配", "2026-11-23 10:00:00", "gm1@lineworks-local.info", "gm1")
    recall = h("前段戻し", "購買手配", "総務部長", "2026-11-23 10:05:00", "gm1@lineworks-local.info", "gm1")
    req = make_request(routeJson=ROUTE4, currentStep="総務部長")
    st = stamps_by_step(req, [SUBMIT, SUP_OK, QUOTE, gm_to_purchasing, recall])
    assert st["上席"].acted and st["購買見積"].acted
    assert not st["総務部長"].acted and not st["購買手配"].acted


def test_previous_round_stamps_hidden_after_resubmit():
    returned = h("差戻", "上席", "申請者", "2026-11-20 10:00:00", "sup1@lineworks-local.info", "sup1", "直して")
    resubmit = h("再申請", "申請者", "上席", "2026-11-22 09:00:00", "applicant@lineworks-local.info", "applicant")
    req = make_request(routeJson=ROUTE4, currentStep="上席")
    st = stamps_by_step(req, [SUBMIT, returned, resubmit])
    assert st["申請者"].date == "令8. 11. 22"  # 再申請の日
    assert not st["上席"].acted  # 前の回の差戻しの印は出さない


def test_returned_request_shows_ng_stamp_and_watermark():
    returned = h("差戻", "上席", "申請者", "2026-11-20 10:00:00", "sup1@lineworks-local.info", "sup1", "直して")
    req = make_request(routeJson=ROUTE4, status="差戻し", currentStep="申請者")
    st = stamps_by_step(req, [SUBMIT, returned])
    assert st["上席"].acted and st["上席"].ng
    view = build_pdf_view(req, make_items(), [SUBMIT, returned], make_masters(), ORIGIN)
    assert view["watermark"] == "差戻"
    assert 'class="stamp acted ng"' in render_pdf_html(view)


def test_completed_request_shows_all_stamps():
    req = make_request(routeJson=ROUTE4, status="完了", currentStep="完了済", totalAmount=3600)
    hist = [SUBMIT, SUP_OK, QUOTE, h("承認", "総務部長", "購買手配", "2026-11-23 10:00:00", "gm1@lineworks-local.info", "gm1"), DONE]
    st = stamps_by_step(req, hist)
    assert all(s.acted for s in st.values())
    assert st["購買手配"].name == "五十嵐"
    view = build_pdf_view(req, make_items(), hist, make_masters(), ORIGIN)
    assert view["watermark"] == "完了"
    assert view["subtotal"] == "¥3,600" and view["tax"] == "¥360" and view["gross"] == "¥3,960"


def test_html_content_escaping_and_file_name():
    items = make_items()
    items[0]["name"] = "<script>alert(1)</script>"
    req = make_request(applicantName='申請/太郎:"x"', reasonDetail="A & B")
    html = render_pdf_html(build_pdf_view(req, items, [SUBMIT], make_masters(), ORIGIN))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "A &amp; B" in html
    assert pdf_file_name(req) == "REQ-20261124093000-123456_申請_太郎__x__貯蔵品購入申請書.pdf"
    assert pdf_file_name(make_request(applicantName="")) == "REQ-20261124093000-123456_申請者_貯蔵品購入申請書.pdf"
    # 明細は行番号順、単価 0 は未定
    view = build_pdf_view(req, items, [SUBMIT], make_masters(), ORIGIN)
    assert [it["no"] for it in view["items"]] == [1, 2]
    assert view["items"][0]["unitPrice"] == "¥1,200" and view["items"][1]["unitPrice"] == "未定"
    assert view["items"][1]["quantity"] == "1.5" and view["items"][0]["quantity"] == "3"
    assert view["totalText"] == "未確定"
    assert build_pdf_view(make_request(amountWaived="true"), [], [], make_masters(), ORIGIN)["totalText"] == "金額確定不要（至急）"


def test_qr_code_points_to_detail_url():
    view = build_pdf_view(make_request(), make_items(), [SUBMIT], make_masters(), ORIGIN)
    assert view["detailUrl"] == ORIGIN + "/?requestId=REQ-20261124093000-123456"
    html = render_pdf_html(view)
    assert "<svg" in html and 'class="barcode"' not in html
    import segno  # 同じ URL から作った QR と同じ形であること

    expected = segno.make_qr(view["detailUrl"], error="m").svg_inline(scale=2, border=0, dark="#0e0e0e")
    assert expected in html


def test_long_history_is_trimmed_and_density_rises():
    hist = [SUBMIT] + [h("更新", "上席", "上席", f"2026-11-21 09:{i:02d}:00", "applicant@lineworks-local.info", "a", "明細1 数量: 1→2")
                       for i in range(30)]
    items = make_items() * 8  # 16 行
    view = build_pdf_view(make_request(), items, hist, make_masters(), ORIGIN)
    assert view["historyCount"] == 31
    assert view["historyOmitted"] == 31 - HISTORY_MAX
    assert len(view["history"]) == HISTORY_MAX
    assert view["history"][-1]["happenedAt"] == "2026-11-21 09:29:00"  # 新しい方を残す
    assert view["density"] == 2
    html = render_pdf_html(view)
    assert "dense-2" in html and "これより前の履歴 11 件は省略" in html
    assert build_pdf_view(make_request(), make_items(), [SUBMIT], make_masters(), ORIGIN)["density"] == 0


def test_era_date():
    assert era_date("2026-05-27 10:00:00") == "令8. 5. 27"
    assert era_date(datetime(2026, 5, 26, 15, 0, tzinfo=timezone.utc)) == "令8. 5. 27"
    assert era_date("2018-12-31") == "18. 12. 31"
    assert era_date("") == ""


def test_pdf_page_blocks_everything_but_data_urls():
    from types import SimpleNamespace

    from app.services.pdf import block_non_data_request

    calls = []

    def route(url):
        return SimpleNamespace(request=SimpleNamespace(url=url), continue_=lambda: calls.append(("go", url)),
                               abort=lambda: calls.append(("stop", url)))

    for url in ("data:image/png;base64,AAAA", "https://example.com/x.png", "http://169.254.169.254/", "file:///etc/passwd"):
        block_non_data_request(route(url))
    assert calls == [("go", "data:image/png;base64,AAAA"), ("stop", "https://example.com/x.png"),
                     ("stop", "http://169.254.169.254/"), ("stop", "file:///etc/passwd")]


# ---------------------------------------------------------------------------
# Chromium（VPS で必ず回す）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def renderer():
    from app.services.pdf import PdfRenderer

    r = PdfRenderer()
    try:
        r._ensure()
    except Exception as error:  # Chromium が無い PC では飛ばす
        pytest.skip(f"Chromium を起動できません: {error}")
    yield r
    r.close()


def page_count(data: bytes) -> int:
    try:
        from pypdf import PdfReader
        import io

        return len(PdfReader(io.BytesIO(data)).pages)
    except ImportError:
        return len(re.findall(rb"/Type\s*/Page[^s]", data))


@pytest.mark.chromium
@pytest.mark.parametrize("n_items,n_hist,comment_len", [(1, 2, 5), (6, 12, 30), (12, 25, 60), (25, 40, 200)])
def test_pdf_is_always_one_page(renderer, n_items, n_hist, comment_len):
    base = make_items()[1]
    items = [dict(base, lineNo=i + 1, name=f"品名{i}" * 3, model="型式-" + str(i) * 10, maker="メーカー", note="備考" * 10)
             for i in range(n_items)]
    hist = [SUBMIT] + [h("更新", "上席", "上席", f"2026-11-21 10:{i % 60:02d}:00", "a@x", "担当 者", "長いコメント" * (comment_len // 6))
                       for i in range(n_hist)]
    req = make_request(routeJson=ROUTE5, reasonDetail="理由" * 200)
    data = renderer.render(render_pdf_html(build_pdf_view(req, items, hist, make_masters(), ORIGIN)))
    assert data.startswith(b"%PDF")
    assert page_count(data) == 1


@pytest.mark.chromium
def test_pdf_japanese_glyphs_render(renderer):
    data = renderer.render(render_pdf_html(build_pdf_view(make_request(), make_items(), [SUBMIT], make_masters(), ORIGIN)))
    assert page_count(data) == 1
    assert len(data) > 10000
