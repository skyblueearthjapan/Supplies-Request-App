"""送信の形（RFC 2047 / RFC 2231）と、staging の転送・本番のテスト転送。"""

from __future__ import annotations

import base64
import email
from datetime import datetime, timedelta, timezone
from email import policy

import pytest

from app.services.mail import (
    GmailApiTransport,
    MailConfigError,
    apply_redirect,
    build_mime,
    effective_redirect,
    format_mail_redirect,
    parse_mail_redirect,
    send_mail,
)
from app.services.mailpdf_common import MailPdfConfig
from app.services.recipients import OutgoingMail

NOW = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)  # 日本時間 12:00
STAGING = MailPdfConfig(app_env="staging", mail_redirect_to="imaizumi@lineworks-local.info")
PROD = MailPdfConfig(app_env="production", mail_redirect_to="")


def sample(attach=False):
    return OutgoingMail(kind="quote_request", to=["mech@lineworks-local.info"], cc=["elec@lineworks-local.info", "ga2@lineworks-local.info"],
                        subject="【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] REQ-1", body="本文です\n2 行目", attach_pdf=attach,
                        request_id="REQ-1")


class FakeTransport:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return f"gmail-{len(self.sent)}"


def parse(msg):
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


def test_mime_headers_are_encoded():
    msg = build_mime(sample(attach=True), PROD, ("REQ-1_申請 太郎_貯蔵品購入申請書.pdf", b"%PDF-1.4 test"))
    raw = msg.as_bytes().decode("ascii")  # 7bit で通ること（日本語はすべて符号化）
    assert "Subject: =?utf-8?" in raw  # RFC 2047
    assert 'From: =?utf-8?' in raw and "<imaizumi@lineworks-local.info>" in raw
    assert "filename*=utf-8''REQ-1_%E7%94%B3" in raw  # RFC 2231
    back = parse(msg)
    assert back["Subject"] == "【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] REQ-1"
    assert back["From"].addresses[0].display_name == "貯蔵品購入申請"
    assert back["To"] == "mech@lineworks-local.info"
    assert back["Cc"] == "elec@lineworks-local.info, ga2@lineworks-local.info"
    parts = list(back.iter_parts())
    assert parts[0].get_content().replace("\r\n", "\n") == "本文です\n2 行目\n"
    assert parts[1].get_filename() == "REQ-1_申請 太郎_貯蔵品購入申請書.pdf"
    assert parts[1].get_content() == b"%PDF-1.4 test"


def test_staging_always_redirects_and_keeps_attachment():
    t = FakeTransport()
    mid = send_mail(sample(attach=True), t, STAGING, None, ("a.pdf", b"PDF"), NOW)
    assert mid == "gmail-1"
    back = parse(t.sent[0])
    assert back["To"] == "imaizumi@lineworks-local.info"
    assert back["Cc"] is None  # Cc は落とす
    assert back["Subject"] == "【テスト転送】【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] REQ-1"
    body = list(back.iter_parts())[0].get_content().replace("\r\n", "\n")
    assert body.startswith("━━━━━━━━━━━━━━━━━━━━━━\n【テスト転送】このメールは本来の宛先には送信されていません。\n"
                           "本来の To : mech@lineworks-local.info\n"
                           "本来の Cc : elec@lineworks-local.info,ga2@lineworks-local.info\n")
    assert body.endswith("本文です\n2 行目\n")
    assert list(back.iter_parts())[1].get_content() == b"PDF"  # 添付は残す


def test_staging_without_redirect_target_refuses():
    with pytest.raises(MailConfigError):
        send_mail(sample(), FakeTransport(), MailPdfConfig(app_env="staging", mail_redirect_to=""), None, None, NOW)
    # 本番のテスト転送の設定があっても、staging は MAIL_REDIRECT_TO だけを使う
    r = effective_redirect(STAGING, "someone@lineworks-local.info|99999999999999", NOW)
    assert r.email == "imaizumi@lineworks-local.info" and r.until is None


def test_production_sends_to_real_recipients_without_redirect():
    t = FakeTransport()
    send_mail(sample(), t, PROD, "", None, NOW)
    back = parse(t.sent[0])
    assert back["To"] == "mech@lineworks-local.info"
    assert back["Cc"] == "elec@lineworks-local.info, ga2@lineworks-local.info"
    assert not back["Subject"].startswith("【テスト転送】")


def test_production_test_redirect_with_expiry():
    raw = format_mail_redirect("Imaizumi@lineworks-local.info", None, NOW)
    assert raw == "imaizumi@lineworks-local.info|" + str(int((NOW + timedelta(minutes=120)).timestamp() * 1000))
    r = parse_mail_redirect(raw, NOW)
    assert r.email == "imaizumi@lineworks-local.info"
    t = FakeTransport()
    send_mail(sample(), t, PROD, raw, None, NOW)
    body = parse(t.sent[0]).get_content()
    assert "転送の失効: 2026-11-24 14:00" in body  # 日本時間
    # 期限切れ・壊れた値は無効（本来の宛先へ）
    assert parse_mail_redirect(raw, NOW + timedelta(minutes=121)) is None
    assert parse_mail_redirect("imaizumi@lineworks-local.info", NOW) is None
    assert parse_mail_redirect("|123", NOW) is None
    assert parse_mail_redirect(format_mail_redirect("x@lineworks-local.info", 5, NOW), NOW + timedelta(minutes=6)) is None


def test_apply_redirect_without_cc_shows_none():
    m = apply_redirect(OutgoingMail(kind="k", to=["a@x"], subject="s", body="b"), effective_redirect(STAGING))
    assert "本来の Cc : （なし）" in m.body and m.cc == []


def test_attachment_only_when_mail_wants_it():
    t = FakeTransport()
    send_mail(sample(attach=False), t, PROD, None, ("a.pdf", b"PDF"), NOW)
    assert not t.sent[0].is_multipart()


def test_gmail_api_transport_posts_raw():
    calls = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "msg-123"}

    class Session:
        def post(self, url, json=None, timeout=None):
            calls.append((url, json))
            return Resp()

    transport = GmailApiTransport(lambda: "creds", session_factory=lambda creds: Session())
    msg = build_mime(sample(), PROD)
    assert transport.send(msg) == "msg-123"
    url, body = calls[0]
    assert url.endswith("/users/me/messages/send")
    assert base64.urlsafe_b64decode(body["raw"]) == msg.as_bytes()
