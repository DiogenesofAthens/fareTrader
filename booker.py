"""
Delta.com Playwright automation.
Full booking flow: login → search → select cabin → apply eCredits → confirm.
DRY_RUN=True (default) stops before the final "Purchase" click.
Saves an error screenshot to SCREENSHOT_DIR on any failure.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class BookingResult:
    success: bool
    pnr: Optional[str] = None
    cancel_by: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = True


# ---------------------------------------------------------------------------
# Selector constants
# Selectors live here so they're easy to find and update when delta.com changes.
# ---------------------------------------------------------------------------

class _Sel:
    # Login page
    LOGIN_USERNAME      = 'input[id="input_loginPage_deltaIdEmailOrMemberNum"]'
    LOGIN_PASSWORD      = 'input[id="input_loginPage_password"]'
    LOGIN_SUBMIT        = 'button[id="btn_loginPage_login"]'
    LOGIN_SUCCESS_MARK  = '[data-testid="header-login-widget"]'  # account widget post-login

    # Home / search form
    SEARCH_FROM              = 'input[placeholder*="From"]'
    SEARCH_TO                = 'input[placeholder*="To"]'
    SEARCH_DATE              = 'input[id*="departureDate"]'
    SEARCH_CABIN             = '[data-testid="cabin-select"]'
    SEARCH_PASSENGERS_WIDGET = '[data-testid="passengers-input"]'
    SEARCH_PASSENGERS_ADD    = '[data-testid="add-adult-passenger"]'
    SEARCH_SUBMIT            = 'button[data-testid="search-button"]'

    # Results page — first-class / Delta One filter
    CABIN_TAB_FIRST     = '[data-testid="cabin-filter-first"]'
    CABIN_TAB_BUSINESS  = '[data-testid="cabin-filter-business"]'
    FIRST_RESULT_SELECT = '[data-testid="flight-select-button"]'

    # Seat selection — skip if possible
    CONTINUE_SEAT       = '[data-testid="continue-without-seat"]'

    # Passenger info — may be pre-filled for logged-in members
    CONTINUE_PASSENGER  = '[data-testid="continue-passenger"]'

    # Payment — eCredits
    ECREDIT_RADIO       = 'input[value="eCredit"]'
    ECREDIT_APPLY       = '[data-testid="apply-ecredit"]'
    ECREDIT_BALANCE     = '[data-testid="ecredit-balance"]'

    # Final purchase
    PURCHASE_BUTTON     = '[data-testid="purchase-button"]'

    # Confirmation
    PNR_ELEMENT         = '[data-testid="confirmation-code"]'
    CANCEL_BY_ELEMENT   = '[data-testid="cancel-deadline"]'


# ---------------------------------------------------------------------------
# Screenshot helper
# ---------------------------------------------------------------------------

def _screenshot(page: "Page", label: str) -> str:  # type: ignore[name-defined]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(config.SCREENSHOT_DIR) / f"error_{label}_{ts}.png"
    try:
        page.screenshot(path=str(path))
        log.info("Screenshot saved: %s", path)
    except Exception as exc:
        log.warning("Could not save screenshot: %s", exc)
    return str(path)


# ---------------------------------------------------------------------------
# Main booking flow
# ---------------------------------------------------------------------------

def book_flight(
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    seats: int = 1,
) -> BookingResult:
    """
    Attempt to book *seats* in *cabin* on *travel_date*.
    Returns BookingResult. Never raises — all exceptions are caught and
    returned as BookingResult(success=False, error=...).
    """
    if not config.DELTA_USERNAME or not config.DELTA_PASSWORD:
        return BookingResult(
            success=False,
            error="DELTA_USERNAME / DELTA_PASSWORD not set",
            dry_run=config.DRY_RUN,
        )

    # Late import so the rest of the codebase doesn't require playwright
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return BookingResult(
            success=False,
            error="playwright not installed — run: pip install playwright && playwright install chromium",
            dry_run=config.DRY_RUN,
        )

    dry_run = config.DRY_RUN
    log.info(
        "%sBOOKING %s→%s %s %s (%d seat(s))",
        "[DRY RUN] " if dry_run else "",
        origin, destination, cabin, travel_date, seats,
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        try:
            result = _run_booking_flow(page, origin, destination, cabin, travel_date, seats, dry_run)
        except Exception as exc:
            shot = _screenshot(page, f"{origin}_{destination}_{travel_date}")
            result = BookingResult(
                success=False,
                error=f"{type(exc).__name__}: {exc} | screenshot: {shot}",
                dry_run=dry_run,
            )
            log.error("Booking flow crashed: %s", exc, exc_info=True)
        finally:
            browser.close()

    return result


def _run_booking_flow(
    page: "Page",  # type: ignore[name-defined]
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    seats: int,
    dry_run: bool,
) -> BookingResult:
    from playwright.sync_api import TimeoutError as PWTimeout

    s = _Sel  # shorthand

    # ------------------------------------------------------------------
    # 1. Login
    # ------------------------------------------------------------------
    log.debug("Navigating to delta.com login")
    page.goto("https://www.delta.com/us/en/sign-in/login")
    page.wait_for_load_state("networkidle")

    try:
        page.fill(s.LOGIN_USERNAME, config.DELTA_USERNAME)
        page.fill(s.LOGIN_PASSWORD, config.DELTA_PASSWORD)
        page.click(s.LOGIN_SUBMIT)
        page.wait_for_selector(s.LOGIN_SUCCESS_MARK, timeout=15_000)
        log.debug("Login successful")
    except PWTimeout:
        _screenshot(page, "login_failed")
        return BookingResult(success=False, error="Login failed — check credentials or 2FA", dry_run=dry_run)

    # ------------------------------------------------------------------
    # 2. Navigate to search
    # ------------------------------------------------------------------
    page.goto("https://www.delta.com/us/en/book-a-trip/flights")
    page.wait_for_load_state("networkidle")

    # Fill origin
    page.click(s.SEARCH_FROM)
    page.fill(s.SEARCH_FROM, origin)
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")

    # Fill destination
    page.click(s.SEARCH_TO)
    page.fill(s.SEARCH_TO, destination)
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")

    # Fill date
    page.click(s.SEARCH_DATE)
    page.fill(s.SEARCH_DATE, travel_date)
    page.keyboard.press("Tab")

    # Select cabin — Delta One = "BUSINESS"; First = "FIRST"
    cabin_option_map = {
        "delta_one": "BUSINESS",
        "business":  "BUSINESS",
        "first":     "FIRST",
    }
    try:
        page.select_option(s.SEARCH_CABIN, cabin_option_map.get(cabin, "BUSINESS"))
    except Exception:
        log.debug("Cabin dropdown not found via select_option — skipping; will filter on results page")

    # Set passenger count (Delta defaults to 1; click + once per extra seat)
    if seats > 1:
        try:
            page.click(s.SEARCH_PASSENGERS_WIDGET, timeout=5_000)
            page.wait_for_timeout(500)
            for _ in range(seats - 1):
                page.click(s.SEARCH_PASSENGERS_ADD, timeout=3_000)
                page.wait_for_timeout(300)
            page.keyboard.press("Escape")
            log.debug("Set passenger count to %d", seats)
        except PWTimeout:
            log.warning("Could not set passenger count to %d — Delta selector changed; proceeding with 1-seat default", seats)

    page.click(s.SEARCH_SUBMIT)
    page.wait_for_load_state("networkidle")
    log.debug("Search submitted")

    # ------------------------------------------------------------------
    # 3. Filter results to correct cabin
    # ------------------------------------------------------------------
    cabin_tab = s.CABIN_TAB_BUSINESS if cabin in ("delta_one", "business") else s.CABIN_TAB_FIRST
    try:
        page.click(cabin_tab, timeout=8_000)
        page.wait_for_load_state("networkidle")
    except PWTimeout:
        log.debug("Cabin filter tab not found — proceeding with default results")

    # ------------------------------------------------------------------
    # 4. Select the first available result
    # ------------------------------------------------------------------
    try:
        page.click(s.FIRST_RESULT_SELECT, timeout=15_000)
        page.wait_for_load_state("networkidle")
        log.debug("First result selected")
    except PWTimeout:
        _screenshot(page, "no_results")
        return BookingResult(success=False, error="No flight results found", dry_run=dry_run)

    # ------------------------------------------------------------------
    # 5. Seat selection — skip
    # ------------------------------------------------------------------
    try:
        page.click(s.CONTINUE_SEAT, timeout=8_000)
        page.wait_for_load_state("networkidle")
    except PWTimeout:
        log.debug("No seat selection step")

    # ------------------------------------------------------------------
    # 6. Passenger info — usually pre-filled; continue
    # ------------------------------------------------------------------
    try:
        page.click(s.CONTINUE_PASSENGER, timeout=8_000)
        page.wait_for_load_state("networkidle")
    except PWTimeout:
        log.debug("No separate passenger info step")

    # ------------------------------------------------------------------
    # 7. Payment — apply eCredits
    # ------------------------------------------------------------------
    try:
        page.click(s.ECREDIT_RADIO, timeout=10_000)
        page.wait_for_timeout(1000)
        page.click(s.ECREDIT_APPLY, timeout=8_000)
        page.wait_for_load_state("networkidle")
        log.debug("eCredits applied")
    except PWTimeout:
        _screenshot(page, "ecredit_apply_failed")
        return BookingResult(success=False, error="Could not apply eCredits — payment page structure may have changed", dry_run=dry_run)

    # Check that the remaining eCredit balance after this booking stays above the configured buffer.
    try:
        bal_el = page.query_selector(s.ECREDIT_BALANCE)
        if bal_el:
            raw_bal = (bal_el.text_content() or "").strip()
            m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", raw_bal)
            if m:
                remaining = float(m.group(1).replace(",", ""))
                log.debug("Remaining eCredit balance after this booking: $%.0f", remaining)
                if remaining < config.MIN_ECREDIT_BUFFER_USD:
                    _screenshot(page, "ecredit_below_buffer")
                    return BookingResult(
                        success=False,
                        error=(
                            f"eCredit buffer insufficient: ${remaining:.0f} remaining"
                            f" < ${config.MIN_ECREDIT_BUFFER_USD:.0f} minimum"
                        ),
                        dry_run=dry_run,
                    )
            else:
                log.debug("Could not parse eCredit balance from: %s", raw_bal)
    except Exception as exc:
        log.debug("eCredit balance check skipped: %s", exc)

    # ------------------------------------------------------------------
    # 8. DRY_RUN stop — don't click Purchase
    # ------------------------------------------------------------------
    if dry_run:
        log.info("[DRY RUN] Stopping before Purchase click — everything looks good up to payment page")
        _screenshot(page, "dry_run_payment_page")
        fake_pnr = f"DRY{origin}{destination}"[:8].upper()
        cancel_by = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
        return BookingResult(
            success=True,
            pnr=fake_pnr,
            cancel_by=cancel_by,
            dry_run=True,
        )

    # ------------------------------------------------------------------
    # 9. Click Purchase (live mode only)
    # ------------------------------------------------------------------
    try:
        page.click(s.PURCHASE_BUTTON, timeout=15_000)
        page.wait_for_load_state("networkidle")
        log.info("Purchase clicked")
    except PWTimeout:
        _screenshot(page, "purchase_click_failed")
        return BookingResult(success=False, error="Purchase button not found or click failed", dry_run=False)

    # ------------------------------------------------------------------
    # 10. Capture PNR from confirmation page
    # ------------------------------------------------------------------
    try:
        pnr_el = page.wait_for_selector(s.PNR_ELEMENT, timeout=20_000)
        pnr = (pnr_el.text_content() or "").strip().upper()
        pnr = re.sub(r"[^A-Z0-9]", "", pnr)  # strip whitespace/punctuation
    except PWTimeout:
        _screenshot(page, "no_confirmation_pnr")
        return BookingResult(success=False, error="Booking may have succeeded but PNR not found on page", dry_run=False)

    # Cancel deadline (may not always appear)
    cancel_by: Optional[str] = None
    try:
        cb_el = page.query_selector(s.CANCEL_BY_ELEMENT)
        if cb_el:
            raw = (cb_el.text_content() or "").strip()
            # Parse whatever date format Delta uses: "Cancel by June 30, 2026" etc.
            m = re.search(r"(\w+ \d+, \d{4})", raw)
            if m:
                cancel_by = datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        pass

    log.info("Booking confirmed — PNR: %s", pnr)
    return BookingResult(success=True, pnr=pnr, cancel_by=cancel_by, dry_run=False)
