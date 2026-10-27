"""Email providers. A missing configuration does not raise."""

import base64
import smtplib
from email.message import EmailMessage

NOT_CONFIGURED = "not_sent: email provider not configured"


class EmailProvider:
    def send(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        attachments: list[tuple[str, bytes, str]],
    ) -> str:
        raise NotImplementedError


class ResendEmail(EmailProvider):
    def __init__(self, api_key: str, sender: str) -> None:
        self._api_key = api_key
        self._sender = sender

    def send(self, *, to, subject, text, attachments) -> str:
        import httpx

        payload = {
            "from": self._sender,
            "to": [to],
            "subject": subject,
            "text": text,
            "attachments": [
                {
                    "filename": name,
                    "content": base64.b64encode(content).decode("ascii"),
                    "content_type": mime,
                }
                for name, content, mime in attachments
            ],
        }
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return str(response.json().get("id") or "sent")


class SmtpEmail(EmailProvider):
    def __init__(self, host: str, port: int, sender: str, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password

    def send(self, *, to, subject, text, attachments) -> str:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        for name, content, mime in attachments:
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
    resend_key = env_text("RESEND_API_KEY")
    if resend_key and sender:
        return ResendEmail(resend_key, sender)
    host = env_text("SMTP_HOST")
    if host and sender:
        port_text = env_text("SMTP_PORT") or "587"
        try:
            port = int(port_text)
        except ValueError:
            port = 587
        return SmtpEmail(host, port, sender, env_text("SMTP_USER"), env_text("SMTP_PASSWORD"))
    return None
