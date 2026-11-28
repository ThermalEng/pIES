"""SMTP delivery helper for stateless one-time verification codes."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from pies_market_gateway.config import GatewaySettings


class MailerDeliveryError(Exception):
    """Sanitized SMTP delivery failure never retaining recipient email addresses."""


def send_otp_email(
    *,
    settings: GatewaySettings,
    to_email: str,
    otp_code: str,
    ttl_seconds: int,
) -> None:
    """Send a short-lived OTP verification code over SMTP without storing plaintext emails."""
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to_email
    msg["Subject"] = "pIES Market verification code"
    msg.set_content(
        (
            f"Your pIES Market verification code is: {otp_code}\n\n"
            f"This code expires in {ttl_seconds} seconds.\n"
            "Do not reply to this automated message.\n"
        ),
        charset="utf-8",
    )

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError):
        # Suppress raw SMTP exception chain (SMTPRecipientsRefused embeds plaintext email).
        raise MailerDeliveryError("Failed to deliver verification email via SMTP") from None
