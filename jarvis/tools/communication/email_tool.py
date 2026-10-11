"""Email: read a draft, send over SMTP, optionally attach files.

Sending is **always** gated behind an explicit confirmation, because an agent
that can email on its own is an agent that can embarrass you.  Configuration
comes from the environment (never hard-coded):

    JARVIS_SMTP_HOST, JARVIS_SMTP_PORT, JARVIS_SMTP_USER, JARVIS_SMTP_PASSWORD,
    JARVIS_EMAIL_FROM, JARVIS_SMTP_TLS=1
"""

from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ...config import get_env
from ...utils.logger import get_logger
from ...utils.sanitizer import truncate
from ..base import tool

log = get_logger("comms.email")

__all__ = ["EmailConfig", "send_email", "draft_email", "EMAIL_TOOLS"]


@dataclass
class EmailConfig:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    sender: str = ""
    use_tls: bool = True

    @classmethod
    def from_env(cls) -> "EmailConfig":
        return cls(
            host=get_env("JARVIS_SMTP_HOST"),
            port=int(get_env("JARVIS_SMTP_PORT", "587") or 587),
            user=get_env("JARVIS_SMTP_USER"),
            password=get_env("JARVIS_SMTP_PASSWORD"),
            sender=get_env("JARVIS_EMAIL_FROM") or get_env("JARVIS_SMTP_USER"),
            use_tls=get_env("JARVIS_SMTP_TLS", "1") not in ("0", "false", "no"),
        )

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)


def build_message(to: str | Sequence[str], subject: str, body: str, *,
                  config: Optional[EmailConfig] = None, html: bool = False,
                  cc: str | Sequence[str] = "", attachments: Sequence[str] = (),
                  reply_to: str = "") -> EmailMessage:
    config = config or EmailConfig.from_env()
    message = EmailMessage()
    message["From"] = config.sender
    recipients = [to] if isinstance(to, str) else list(to)
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    if reply_to:
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    message.set_content("This message requires an HTML-capable client."
                        if html else body)
    if html:
        message.add_alternative(body, subtype="html")
    for attachment in attachments:
        path = Path(attachment).expanduser()
        if not path.exists():
            log.warning(f"attachment missing: {path}")
            continue
        mime, _ = mimetypes.guess_type(str(path))
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                               filename=path.name)
    return message


def send_now(message: EmailMessage, *, config: Optional[EmailConfig] = None) -> Dict[str, Any]:
    config = config or EmailConfig.from_env()
    if not config.configured:
        return {"ok": False, "error": "SMTP not configured (set JARVIS_SMTP_HOST, "
                                     "JARVIS_SMTP_USER, JARVIS_SMTP_PASSWORD)"}
    try:
        if config.port == 465:
            with smtplib.SMTP_SSL(config.host, config.port,
                                  context=ssl.create_default_context(),
                                  timeout=25) as server:
                if config.user:
                    server.login(config.user, config.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(config.host, config.port, timeout=25) as server:
                server.ehlo()
                if config.use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if config.user:
                    server.login(config.user, config.password)
                server.send_message(message)
    except Exception as exc:
        log.warning(f"send_email failed: {exc}")
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "to": message["To"], "subject": message["Subject"]}


@tool(category="communication", timeout=30)
def draft_email(to: str, subject: str, body: str, cc: str = "",
                attachments: str = "") -> str:
    """Compose an email and show it for approval — does NOT send it.

    Always do this first, then call `send_email` with the same content once
    Heoster approves.

    Args:
        to: Recipient address (comma-separate for several)
        subject: Subject line
        body: Plain-text body
        cc: Optional CC addresses
        attachments: Optional comma-separated file paths
    """
    config = EmailConfig.from_env()
    status = "configured" if config.configured else \
        "⚠ SMTP not configured (JARVIS_SMTP_HOST/USER/PASSWORD missing)"
    attachment_list = [a.strip() for a in attachments.split(",") if a.strip()]
    return "\n".join([
        "**Email draft** (not sent)",
        f"From    : {config.sender or '(not configured)'}",
        f"To      : {to}",
        f"Cc      : {cc or '-'}",
        f"Subject : {subject}",
        f"Attach  : {', '.join(attachment_list) or '-'}",
        f"SMTP    : {status}",
        "",
        "--- body ---",
        truncate(body, 2000),
        "--- end ---",
        "",
        "Reply with approval to send (I'll call `send_email` with confirm=true).",
    ])


@tool(category="communication", timeout=90)
def send_email(to: str, subject: str, body: str, confirm: bool = False,
               cc: str = "", html: bool = False, attachments: str = "") -> str:
    """Actually send an email over SMTP. Requires confirm=true.

    Only set confirm=true after Heoster has explicitly approved the exact
    recipient, subject and body (use `draft_email` first).

    Args:
        to: Recipient address(es), comma-separated
        subject: Subject line
        body: Message body
        confirm: Must be true to send — this is the human-approval gate
        cc: Optional CC addresses
        html: Treat the body as HTML
        attachments: Comma-separated file paths to attach
    """
    if not confirm:
        return ("🚫 Refusing to send without confirmation. Use `draft_email` first, "
                "then call this again with confirm=true once Heoster approves.")
    config = EmailConfig.from_env()
    attachment_list = [a.strip() for a in attachments.split(",") if a.strip()]
    try:
        message = build_message(
            [address.strip() for address in to.split(",") if address.strip()],
            subject, body, config=config, html=html,
            cc=[c.strip() for c in cc.split(",") if c.strip()],
            attachments=attachment_list)
    except Exception as exc:
        return f"Could not build the email: {exc}"
    result = send_now(message, config=config)
    if not result["ok"]:
        return f"❌ Email failed: {result['error']}"
    return f"✅ Email sent to {result['to']} — “{result['subject']}”"


@tool(category="communication", timeout=15)
def email_status() -> str:
    """Check whether email sending is configured (and what is missing)."""
    config = EmailConfig.from_env()
    if config.configured:
        return (f"SMTP ready: {config.host}:{config.port} as {config.sender} "
                f"(TLS {'on' if config.use_tls else 'off'})")
    missing = [name for name, value in (
        ("JARVIS_SMTP_HOST", config.host), ("JARVIS_SMTP_USER", config.user),
        ("JARVIS_SMTP_PASSWORD", config.password),
        ("JARVIS_EMAIL_FROM/JARVIS_SMTP_USER", config.sender)) if not value]
    return ("Email is not configured. Add these to .env: "
            + ", ".join(missing)
            + "\n(Gmail needs an App Password, not your normal password.)")


EMAIL_TOOLS = [draft_email, send_email, email_status]
