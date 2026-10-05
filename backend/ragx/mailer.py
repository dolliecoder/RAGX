"""Outgoing email over SMTP (optional) with an in-memory mode for tests."""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .config import get_settings

log = logging.getLogger("ragx.mail")

# smtp_host="memory": messages are appended here instead of being sent.
outbox: list[dict[str, str]] = []
_lock = threading.Lock()


def enabled() -> bool:
    return bool(get_settings().smtp_host.strip())


def send(to: str, subject: str, text: str) -> bool:
    """Send a plain-text email. Returns False (and logs) instead of raising, so a
    mail outage never breaks sign-up or sign-in."""
    st = get_settings()
    host = st.smtp_host.strip()
    if not host:
        return False
    sender = st.smtp_from or st.smtp_user or "ragx@localhost"
    if host == "memory":
        with _lock:
            outbox.append({"to": to, "from": sender, "subject": subject, "text": text})
        log.info("email (memory mode, not sent) to %s: %s\n%s", to, subject, text)
        return True
    msg = EmailMessage()
    name, addr = parseaddr(sender)
    msg["From"] = formataddr((name or "RAGX", addr or sender))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    try:
        ctx = ssl.create_default_context()
        if st.smtp_tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, st.smtp_port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(host, st.smtp_port, timeout=20)
        with server:
            if st.smtp_tls == "starttls":
                server.starttls(context=ctx)
            if st.smtp_user:
                server.login(st.smtp_user, st.smtp_password)
            server.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as e:
        log.error("sending email to %s failed: %s", to, e)
        return False


def link(path: str) -> str:
    return get_settings().app_url.rstrip("/") + path


def send_verification(to: str, token: str) -> bool:
    return send(
        to,
        "Confirm your RAGX email address",
        "Hi,\n\nPlease confirm your email address for RAGX by opening this link:\n\n"
        f"{link('/verify?token=' + token)}\n\n"
        "The link is valid for 3 days. If you did not create an account, you can ignore this email.\n",
    )


def send_reset(to: str, token: str) -> bool:
    return send(
        to,
        "Reset your RAGX password",
        "Hi,\n\nSomeone (hopefully you) asked to reset your RAGX password. Choose a new one here:\n\n"
        f"{link('/reset?token=' + token)}\n\n"
        "The link is valid for 1 hour and works once. If you did not ask for this, ignore this email; "
        "your password stays the same.\n",
    )


def send_invite(to: str, token: str, inviter: str) -> bool:
    return send(
        to,
        "You have been invited to RAGX",
        f"Hi,\n\n{inviter} created a RAGX account for you. Set your password to get started:\n\n"
        f"{link('/reset?token=' + token + '&invite=1')}\n\n"
        "The link is valid for 7 days.\n",
    )
