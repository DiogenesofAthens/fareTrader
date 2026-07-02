"""Lead capture — sends early-access requests to the owner via Resend.

Env vars:
    RESEND_API_KEY — Resend API key. When unset, leads are logged and the
    endpoint still returns ok so the form works in local dev.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

NOTIFY_EMAIL = "kwessman@gmail.com"
FROM_ADDRESS = "fareTrader <onboarding@resend.dev>"

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ""))


def send_lead(email: str, note: str = "") -> None:
    """Notify the owner of a new early-access request. Raises on Resend failure."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print(f"[leads] DEV — would capture lead: {email}")
        return

    detail = f"<p><strong>{email}</strong></p>"
    if note:
        detail += f"<p>Note: {note}</p>"

    payload = json.dumps(
        {
            "from": FROM_ADDRESS,
            "to": NOTIFY_EMAIL,
            "subject": f"New fareTrader early-access request: {email}",
            "html": f'<div style="font-family:sans-serif;max-width:480px;">'
                    f"<h2>New fareTrader Early-Access Request</h2>{detail}</div>",
        }
    ).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Resend returned {resp.status}")
