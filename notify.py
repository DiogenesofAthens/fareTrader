"""
Pushover push notifications. Entirely no-op when credentials are absent
so the agent runs fine without a Pushover account.
"""
from __future__ import annotations

import logging
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

# Pushover priority levels
_NORMAL  = 0
_HIGH    = 1
_URGENT  = 2   # requires acknowledge + retry/expire params


def _configured() -> bool:
    return bool(config.PUSHOVER_USER_KEY and config.PUSHOVER_APP_TOKEN)


def _send(title: str, message: str, priority: int = _NORMAL, sound: Optional[str] = None) -> None:
    if not _configured():
        log.debug("Pushover not configured — skipping notification: %s", title)
        return
    payload: dict = {
        "token":   config.PUSHOVER_APP_TOKEN,
        "user":    config.PUSHOVER_USER_KEY,
        "title":   title,
        "message": message,
        "priority": priority,
    }
    if sound:
        payload["sound"] = sound
    if priority == _URGENT:
        payload["retry"]  = 60   # re-alert every 60 s
        payload["expire"] = 3600 # for up to 1 hour
    try:
        r = requests.post(_PUSHOVER_URL, data=payload, timeout=10)
        r.raise_for_status()
        log.debug("Pushover sent: %s", title)
    except Exception as exc:
        log.warning("Pushover delivery failed: %s", exc)


# ---------------------------------------------------------------------------
# Public notification types
# ---------------------------------------------------------------------------

def price_trigger_found(
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    price: float,
    threshold: float,
    auto_book: bool,
) -> None:
    action = "Auto-booking" if auto_book else "Alert only"
    _send(
        title=f"Fare trigger: {origin}→{destination}",
        message=(
            f"{cabin.replace('_',' ').title()} on {travel_date}\n"
            f"${price:,.0f} ≤ threshold ${threshold:,.0f}\n"
            f"{action}"
        ),
        priority=_HIGH,
        sound="cashregister",
    )


def booking_confirmed(
    pnr: str,
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    price: float,
    cancel_by: Optional[str],
    dry_run: bool,
) -> None:
    tag = "[DRY RUN] " if dry_run else ""
    cancel_note = f"\nCancel by: {cancel_by}" if cancel_by else ""
    _send(
        title=f"{tag}Booking confirmed: {origin}→{destination}",
        message=(
            f"PNR: {pnr}\n"
            f"{cabin.replace('_',' ').title()} — {travel_date}\n"
            f"${price:,.0f}{cancel_note}"
        ),
        priority=_HIGH,
        sound="magic",
    )


def cancel_deadline_warning(
    pnr: str,
    origin: str,
    destination: str,
    travel_date: str,
    cancel_by: str,
    days_remaining: int,
) -> None:
    urgent = days_remaining <= 3
    _send(
        title=f"{'URGENT: ' if urgent else ''}Cancel deadline {origin}→{destination}",
        message=(
            f"PNR {pnr} — {travel_date}\n"
            f"Cancel by {cancel_by} ({days_remaining}d remaining)\n"
            f"Visit delta.com to cancel if not flying."
        ),
        priority=_URGENT if urgent else _HIGH,
        sound="siren" if urgent else "intermission",
    )


def scan_summary(
    routes_checked: int,
    dates_checked: int,
    trigger_count: int,
    booking_count: int,
    dry_run: bool,
) -> None:
    """Silent notification (priority -1) — won't wake the device."""
    tag = "[DRY RUN] " if dry_run else ""
    _send(
        title=f"{tag}Scan complete",
        message=(
            f"{routes_checked} routes × {dates_checked} dates scanned\n"
            f"{trigger_count} trigger(s) found, {booking_count} booking(s) made"
        ),
        priority=-1,  # quiet / no alert sound
    )


def error_alert(context: str, error: str) -> None:
    _send(
        title=f"fareTrader error",
        message=f"{context}\n{error[:200]}",
        priority=_HIGH,
        sound="falling",
    )
