"""S11 Send. The recipient lock lives here.

Rules enforced in code, regardless of prompts or data:
- The only recipient is MY_EMAIL from .env. `send_email` has no `to`, `cc`, `bcc` or
  `reply_to` parameter, and the payload is re-checked before it reaches the transport.
- The prospect's name/email may appear only in the subject tag and body.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .config import Settings
from .models import SendInfo

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_ALLOWED_KEYS = {"from", "to", "subject", "text"}


class RecipientLockError(RuntimeError):
    """Raised whenever an email would go anywhere other than MY_EMAIL."""


def _norm(addr: str) -> str:
    return addr.strip().lower()


def locked_recipient(settings: Settings) -> str:
    addr = settings.my_email.strip()
    if not addr or not _EMAIL_RE.match(addr):
        raise RecipientLockError("MY_EMAIL is missing or invalid in .env; refusing to send")
    return addr


def assert_payload_locked(payload: dict[str, Any], my_email: str) -> None:
    """Final gate before any transport call. Fails closed."""
    extra = set(payload) - _ALLOWED_KEYS
    if extra:
        raise RecipientLockError(f"payload has disallowed fields: {sorted(extra)}")
    to = payload.get("to")
    if not isinstance(to, list) or len(to) != 1 or not isinstance(to[0], str):
        raise RecipientLockError("payload must have exactly one recipient")
    if _norm(to[0]) != _norm(my_email):
        raise RecipientLockError("recipient is not MY_EMAIL")
    for key in ("subject", "from"):
        if "\n" in str(payload.get(key, "")) or "\r" in str(payload.get(key, "")):
            raise RecipientLockError(f"newline in {key} (header injection)")


def _one_line(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", s).strip()


def tag_subject(subject: str, prospect_name: str, company: str) -> str:
    return _one_line(f"[TEST → {prospect_name}, {company}] {subject}")


class Transport(Protocol):
    def send(self, payload: dict[str, Any]) -> str: ...


@dataclass
class ResendTransport:
    api_key: str
    my_email: str

    def send(self, payload: dict[str, Any]) -> str:
        assert_payload_locked(payload, self.my_email)  # defence in depth
        if not self.api_key:
            raise RuntimeError("RESEND_API_KEY is not set")
        import resend

        resend.api_key = self.api_key
        result = resend.Emails.send(dict(payload))
        msg_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
        if not msg_id:
            raise RuntimeError("Resend returned no message id")
        return str(msg_id)


def send_email(settings: Settings, *, subject: str, body: str, prospect_name: str,
               company: str, transport: Transport | None = None) -> SendInfo:
    """Send one approved draft to MY_EMAIL. There is deliberately no recipient argument."""
    my_email = locked_recipient(settings)
    payload = {
        "from": _one_line(settings.resend_from),
        "to": [my_email],
        "subject": tag_subject(subject, prospect_name, company),
        "text": body,
    }
    assert_payload_locked(payload, my_email)
    transport = transport or ResendTransport(api_key=settings.resend_api_key, my_email=my_email)
    msg_id = transport.send(payload)
    return SendInfo(message_id=msg_id, sent_at=datetime.now(timezone.utc), provider="resend")
