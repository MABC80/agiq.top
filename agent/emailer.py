"""Email providers. A missing configuration does not raise."""

import base64
import smtplib
from email.message import EmailMessage

NOT_CONFIGURED = "not_sent: email provider not configured"


class EmailProvider:
    reply_to: str = ""

    def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        attachments: list[tuple[str, bytes, str]] | None = None,
        reply_to: str | None = None,
        html: str | None = None,
    ) -> str:
        raise NotImplementedError


def resend_payload(
    *,
    sender: str,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    reply_to: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> dict:
    payload = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        payload["attachments"] = [
            {
                "filename": name,
                "content": base64.b64encode(content).decode("ascii"),
                "content_type": mime,
            }
            for name, content, mime in attachments
        ]
    return payload


class ResendEmail(EmailProvider):
    def __init__(self, api_key: str, sender: str, reply_to: str = "") -> None:
        self._api_key = api_key
        self._sender = sender
        self.reply_to = reply_to

    def send(self, *, to, subject, text, attachments=None, reply_to=None, html=None) -> str:
        import httpx

        chosen = self.reply_to if reply_to is None else reply_to
        payload = resend_payload(
            sender=self._sender,
            to=to,
            subject=subject,
            text=text,
            html=html,
            reply_to=chosen or None,
            attachments=attachments,
        )
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json().get("id") or "sent")


class SmtpEmail(EmailProvider):
    def __init__(self, host: str, port: int, sender: str, username: str, password: str, reply_to: str = "") -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self.reply_to = reply_to

    def send(self, *, to, subject, text, attachments=None, reply_to=None, html=None) -> str:
        chosen = self.reply_to if reply_to is None else reply_to
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        if chosen:
            message["Reply-To"] = chosen
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")
        for name, content, mime in attachments or []:
            main, _, sub = mime.partition("/")
            message.add_attachment(content, maintype=main or "application", subtype=sub or "octet-stream", filename=name)
        with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(message)
        return "sent"


def email_provider_from_env() -> EmailProvider | None:
    from db import env_text

    sender = env_text("EMAIL_FROM")
    reply_to = env_text("EMAIL_REPLY_TO")
    resend_key = env_text("RESEND_API_KEY")
    if resend_key and sender:
        return ResendEmail(resend_key, sender, reply_to)
    host = env_text("SMTP_HOST")
    if host and sender:
        port_text = env_text("SMTP_PORT") or "587"
        try:
            port = int(port_text)
        except ValueError:
            port = 587
        return SmtpEmail(host, port, sender, env_text("SMTP_USER"), env_text("SMTP_PASSWORD"), reply_to)
    return None
