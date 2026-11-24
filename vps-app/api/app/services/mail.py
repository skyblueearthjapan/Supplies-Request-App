"""メールの送信（Gmail API・imaizumi@ の委任トークン）とテスト転送。

- 差出人名は「貯蔵品購入申請」。件名・差出人名の日本語と絵文字は RFC 2047、添付の日本語ファイル名は RFC 2231。
- staging では必ず MAIL_REDIRECT_TO へ寄せる（件名【テスト転送】、本来の To/Cc を本文の頭、Cc は落とす、添付は残す）。
- production でも、管理画面のテスト転送（期限付き）が有効な間は同じ形で寄せる（GAS 版の sendMail_ と同じ）。
- 送信は必ず send_mail() を通すこと（直接 Gmail API を呼ぶと転送を素通りする）。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Callable, Protocol

from .mailpdf_common import MailPdfConfig, normalize_email, parse_number, to_jst
from .recipients import OutgoingMail

log = logging.getLogger(__name__)

# 1 行 998 字まで折り返さない（添付の日本語ファイル名を filename*0*= の分割にせず、filename*= の 1 つで書くため）。
MIME_POLICY = SMTP.clone(max_line_length=998)

MAIL_REDIRECT_DEFAULT_MINUTES = 120
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class MailConfigError(RuntimeError):
    """送ってはいけない設定（staging なのに転送先が無いなど）。"""


@dataclass(frozen=True)
class MailRedirect:
    email: str
    until: datetime | None  # None は期限なし（staging の常時転送）


# ---------------------------------------------------------------------------
# テスト転送（GAS: MAIL_REDIRECT_TO スクリプトプロパティ「転送先|失効エポックミリ秒」と同じ形で設定に持つ）
# ---------------------------------------------------------------------------

def parse_mail_redirect(raw: Any, now: datetime | None = None) -> MailRedirect | None:
    """設定の値「email|失効エポックミリ秒」を読む。期限切れ・壊れた値は無効（安全側）。"""
    parts = str(raw or "").split("|")
    email = normalize_email(parts[0])
    until_ms = parse_number(parts[1]) if len(parts) > 1 else 0
    now = now or datetime.now(timezone.utc)
    if not email or until_ms <= 0 or until_ms <= now.timestamp() * 1000:
        return None
    return MailRedirect(email=email, until=datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc))


def format_mail_redirect(email: str, minutes: Any = None, now: datetime | None = None) -> str:
    """管理画面でテスト転送を有効にするときに設定へ書く値を作る（既定 120 分）。"""
    mins = parse_number(minutes) if parse_number(minutes) > 0 else MAIL_REDIRECT_DEFAULT_MINUTES
    now = now or datetime.now(timezone.utc)
    until_ms = int(now.timestamp() * 1000 + mins * 60 * 1000)
    return normalize_email(email) + "|" + str(until_ms)


def effective_redirect(config: MailPdfConfig, settings_redirect_raw: Any = None,
                       now: datetime | None = None) -> MailRedirect | None:
    """今の送信で使う転送先。staging は常に MAIL_REDIRECT_TO（無ければ送らない）。"""
    if not config.is_production:
        target = normalize_email(config.mail_redirect_to)
        if not target:
            raise MailConfigError("staging では MAIL_REDIRECT_TO が必須です（本来の宛先へ送らないため）。")
        return MailRedirect(email=target, until=None)
    return parse_mail_redirect(settings_redirect_raw, now)


def apply_redirect(mail: OutgoingMail, redirect: MailRedirect | None) -> OutgoingMail:
    """転送するときの形に直す（GAS: sendMail_ と同じ見出し）。"""
    if redirect is None:
        return mail
    until_text = to_jst(redirect.until).strftime("%Y-%m-%d %H:%M") if redirect.until else "なし（ステージング環境は常に転送）"
    header = "\n".join([
        "━━━━━━━━━━━━━━━━━━━━━━",
        "【テスト転送】このメールは本来の宛先には送信されていません。",
        "本来の To : " + (mail.to_header() or "（なし）"),
        "本来の Cc : " + (mail.cc_header() or "（なし）"),
        "転送の失効: " + until_text,
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ])
    return OutgoingMail(kind=mail.kind, to=[redirect.email], cc=[], subject="【テスト転送】" + mail.subject,
                        body=header + mail.body, attach_pdf=mail.attach_pdf, request_id=mail.request_id)


# ---------------------------------------------------------------------------
# MIME の組み立て
# ---------------------------------------------------------------------------

def build_mime(mail: OutgoingMail, config: MailPdfConfig, attachment: tuple[str, bytes] | None = None) -> EmailMessage:
    """1 通の MIME。日本語・絵文字の件名と差出人名は RFC 2047、添付のファイル名は RFC 2231 になる。"""
    msg = EmailMessage(policy=MIME_POLICY)
    msg["From"] = Address(display_name=config.mail_from_name, addr_spec=config.mail_from)
    msg["To"] = ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    msg["Subject"] = mail.subject
    msg.set_content(mail.body, charset="utf-8", cte="base64")
    if attachment is not None:
        name, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
        # policy.SMTP は日本語のファイル名を RFC 2231（filename*=utf-8''...）で書く。
    return msg


def to_gmail_raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# 送信
# ---------------------------------------------------------------------------

class MailTransport(Protocol):
    def send(self, msg: EmailMessage) -> str:
        """送って Gmail のメッセージ ID を返す。失敗は例外。"""


class GmailApiTransport:
    """Gmail API（users.messages.send）で送る。credentials は google_delegate.get_delegate_credentials() の戻り値。"""

    def __init__(self, credentials_provider: Callable[[], Any], timeout: float = 30.0, session_factory: Callable[[Any], Any] | None = None):
        self._credentials_provider = credentials_provider
        self._timeout = timeout
        self._session_factory = session_factory

    def _session(self):
        creds = self._credentials_provider()
        if self._session_factory is not None:
            return self._session_factory(creds)
        from google.auth.transport.requests import AuthorizedSession

        return AuthorizedSession(creds)

    def send(self, msg: EmailMessage) -> str:
        resp = self._session().post(GMAIL_SEND_URL, json={"raw": to_gmail_raw(msg)}, timeout=self._timeout)
        if resp.status_code >= 400:
            raise RuntimeError(f"Gmail API の送信に失敗しました: HTTP {resp.status_code} {resp.text[:300]}")
        return str(resp.json().get("id", ""))


def send_mail(mail: OutgoingMail, transport: MailTransport, config: MailPdfConfig,
              settings_redirect_raw: Any = None, attachment: tuple[str, bytes] | None = None,
              now: datetime | None = None) -> str:
    """1 通送る。転送の判定はここで必ず行う。戻り値は Gmail のメッセージ ID。"""
    redirect = effective_redirect(config, settings_redirect_raw, now)
    actual = apply_redirect(mail, redirect)
    if not actual.to:
        raise ValueError("宛先がありません。")
    if redirect is not None:
        log.info("send_mail: redirected to %s (original to=%s, cc=%s)", redirect.email, mail.to_header(), mail.cc_header())
    msg = build_mime(actual, config, attachment if actual.attach_pdf else None)
    return transport.send(msg)


__all__ = [
    "MailConfigError",
    "MailRedirect",
    "MailTransport",
    "GmailApiTransport",
    "apply_redirect",
    "build_mime",
    "effective_redirect",
    "format_mail_redirect",
    "parse_mail_redirect",
    "send_mail",
    "to_gmail_raw",
]
