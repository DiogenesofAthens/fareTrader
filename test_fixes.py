"""
Regression tests for the eight fixes applied to fareTrader.
Run with:  python test_fixes.py
"""
from __future__ import annotations

import importlib
import sys
import types
from datetime import date, datetime
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PASS = []
_FAIL = []


def _run(fn):
    try:
        fn()
        _PASS.append(fn.__name__)
        print(f"  PASS  {fn.__name__}")
    except Exception as exc:
        _FAIL.append((fn.__name__, exc))
        print(f"  FAIL  {fn.__name__}: {exc}")


# ---------------------------------------------------------------------------
# Fix 1 — seats silently ignored (structural check)
# ---------------------------------------------------------------------------

def test_booker_has_passenger_logic():
    with open("booker.py") as f:
        src = f.read()
    assert "SEARCH_PASSENGERS_WIDGET" in src, "SEARCH_PASSENGERS_WIDGET selector missing"
    assert "SEARCH_PASSENGERS_ADD" in src, "SEARCH_PASSENGERS_ADD selector missing"
    assert "seats - 1" in src, "passenger increment loop (seats - 1) missing"
    assert "seats > 1" in src, "guard 'if seats > 1' missing"


# ---------------------------------------------------------------------------
# Fix 2 — eCredit balance check (structural + logic)
# ---------------------------------------------------------------------------

def test_booker_has_ecredit_balance_check():
    with open("booker.py") as f:
        src = f.read()
    assert "ECREDIT_BALANCE" in src, "ECREDIT_BALANCE selector not referenced"
    assert "MIN_ECREDIT_BUFFER_USD" in src, "MIN_ECREDIT_BUFFER_USD not used in booker"
    assert "remaining < config.MIN_ECREDIT_BUFFER_USD" in src, "balance guard condition missing"


def test_ecredit_balance_parse_logic():
    """Simulate the regex that parses the balance string from Delta's page."""
    import re
    pattern = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

    cases = [
        ("$1,234.56 remaining", 1234.56),
        ("Balance: $500", 500.0),
        ("$0.00", 0.0),
        ("Remaining eCredits $2,000", 2000.0),
    ]
    for text, expected in cases:
        m = pattern.search(text)
        assert m, f"Pattern did not match: {text!r}"
        val = float(m.group(1).replace(",", ""))
        assert abs(val - expected) < 0.01, f"Got {val}, expected {expected} for {text!r}"


# ---------------------------------------------------------------------------
# Fix 3 — scan_route() removed
# ---------------------------------------------------------------------------

def test_scan_route_removed():
    import scanner
    importlib.reload(scanner)
    assert not hasattr(scanner, "scan_route"), "scan_route() should have been removed from scanner.py"


# ---------------------------------------------------------------------------
# Fix 4 — dead URL removed
# ---------------------------------------------------------------------------

def test_dead_url_removed():
    with open("scanner.py") as f:
        src = f.read()
    assert "tfs=CBwQ" not in src, "Hardcoded tfs= URL still present"
    assert "google.com/travel/flights" in src, "Google Flights URL should still be present"
    # The dead first url= assignment inside _scrape_google_flights should be gone.
    # Extract only the _scrape_google_flights function body to count url assignments.
    start = src.index("def _scrape_google_flights(")
    end = src.index("\ndef _scrape_kayak(")
    gf_body = src[start:end]
    url_assignments = [ln for ln in gf_body.splitlines() if ln.strip().startswith("url = (")]
    assert len(url_assignments) == 1, (
        f"Expected 1 url assignment in _scrape_google_flights, found {len(url_assignments)}"
    )


# ---------------------------------------------------------------------------
# Fix 5 — redundant datetime import removed from inside function
# ---------------------------------------------------------------------------

def test_no_redundant_datetime_import():
    with open("booker.py") as f:
        src = f.read()
    assert "from datetime import datetime as _dt" not in src, \
        "Redundant 'from datetime import datetime as _dt' still present inside function"
    # The top-level import must still be there
    assert "from datetime import datetime" in src, \
        "Top-level 'from datetime import datetime' was accidentally removed"


def test_cancel_by_parsing_uses_module_datetime():
    """datetime.strptime should be used, not _dt.strptime."""
    with open("booker.py") as f:
        src = f.read()
    assert "_dt.strptime" not in src, "_dt.strptime still present; should use datetime.strptime"
    assert "datetime.strptime" in src, "datetime.strptime not found"


# ---------------------------------------------------------------------------
# Fix 6 — price bounds normalized
# ---------------------------------------------------------------------------

def test_google_json_price_bounds():
    """Lower bound is now cabin-aware: wrong-cabin (economy) prices scraped
    from a page that ignored the cabin filter must be discarded."""
    import json

    import scanner
    importlib.reload(scanner)

    with open("scanner.py") as f:
        src = f.read()
    assert "price > 100" not in src, "Old loose lower-bound 'price > 100' still present"

    assert scanner._min_plausible("delta_one") == 1500.0
    assert scanner._min_plausible("business") == 1500.0
    assert scanner._min_plausible("first") == 800.0
    assert scanner._min_plausible("unknown_cabin") == 500.0

    # A $900 fare labeled delta_one is economy noise — reject
    fake_html = _gf_label(900, "Nonstop flight with Delta.")
    assert scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04") is None


def _gf_label(price: int, rest: str) -> str:
    """One Google Flights itinerary element, as the page renders it."""
    return f'<li aria-label="From {price} US dollars. {rest} Leaves Atlanta at 4:45 PM."></li>'


def test_google_flights_rejects_low_price():
    """_parse_google_price should reject a Delta fare of 200 (below the cabin floor)."""
    import scanner
    importlib.reload(scanner)

    fake_html = _gf_label(200, "Nonstop flight with Delta.")
    result = scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04")
    assert result is None, f"Expected None for sub-floor price, got {result}"


def test_google_flights_accepts_valid_delta_price():
    """_parse_google_price should accept a Delta nonstop at 3000."""
    import scanner
    importlib.reload(scanner)

    fake_html = _gf_label(3000, "Nonstop flight with Delta.")
    result = scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04")
    assert result is not None, "Expected a Flight for valid price 3000"
    assert result.price_usd == 3000.0


def test_google_flights_counts_only_delta_single_cabin():
    """Other airlines, codeshares operated by others, mixed-cabin itineraries
    and unattributed numbers must not set the price (the Aug–Oct 2026 bug:
    a $3,868 JetBlue mixed-cabin fare recorded as Delta One)."""
    import scanner
    importlib.reload(scanner)

    fake_html = "".join([
        _gf_label(3868, "1 stop flight with JetBlue. Premium Economy + Business Class Layover (1 of 1)."),
        _gf_label(1206, "2 stops flight with Delta. Business Class + Economy Layover (1 of 2)."),
        _gf_label(2500, "Nonstop flight with Air France."),
        _gf_label(2600, "Nonstop flight with Delta. Operated by Air France."),
        _gf_label(2700, "1 stop flight with Delta and KLM."),
        'AF_initDataCallback({key: "x", data: [[1999]] }); <span>$1,800</span>',
        _gf_label(9334, "Nonstop flight with Delta."),
        _gf_label(13957, "Nonstop flight with Delta."),
    ])
    result = scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-10-30")
    assert result is not None and result.price_usd == 9334.0, f"Expected Delta's $9,334, got {result}"

    # Nothing attributable to Delta → no price, not a guess
    other_only = _gf_label(3868, "1 stop flight with JetBlue.")
    assert scanner._parse_google_price(other_only, "ATL", "CDG", "delta_one", "2026-10-30") is None


# ---------------------------------------------------------------------------
# Fix 7 — notify.scan_summary early-return condition
# ---------------------------------------------------------------------------

def test_scan_summary_odd_condition_removed():
    with open("notify.py") as f:
        src = f.read()
    assert "trigger_count == 0 and not _configured()" not in src, \
        "Odd early-return condition still present in scan_summary"


def test_scan_summary_no_crash_without_pushover():
    """scan_summary must not raise when Pushover is not configured."""
    import notify
    importlib.reload(notify)
    # No env vars set → _configured() returns False → _send should no-op
    notify.scan_summary(routes_checked=3, dates_checked=10, trigger_count=0, booking_count=0, dry_run=True)
    notify.scan_summary(routes_checked=3, dates_checked=10, trigger_count=2, booking_count=1, dry_run=False)


# ---------------------------------------------------------------------------
# Fix 8 — break → continue on booking limit
# ---------------------------------------------------------------------------

def test_agent_uses_continue_not_break():
    with open("agent.py") as f:
        src = f.read()
    assert "skipping remaining dates" not in src, \
        "Old 'skipping remaining dates' break message still present"
    assert "skipping booking for this date" in src, \
        "New continue log message not found"


def test_price_history_recorded_past_booking_limit():
    """
    When the per-route booking limit is hit, subsequent dates should still have
    their prices recorded and trigger notifications sent.
    """
    # We test the agent.run_scan() loop in isolation by mocking all I/O.
    import config as cfg

    # Temporarily set MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN to 1
    original_limit = cfg.MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN
    cfg.MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN = 1

    # Build a minimal route that auto-books at a threshold well above our fake price
    route = cfg.Route("ATL", "CDG", "delta_one", max_price=9999.0, auto_book=True)
    original_routes = cfg.ROUTES
    cfg.ROUTES = [route]

    # The booker is only reached when a Delta login is configured (placeholders;
    # book_flight is mocked below).
    original_creds = (cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD)
    cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD = "placeholder-user", "placeholder-pass"

    # Two dates, both below threshold. Must be in the future — run_scan
    # filters past dates, so hardcoded dates rot (this test broke silently
    # when the calendar passed them).
    from datetime import timedelta
    fake_dates = [
        (date.today() + timedelta(days=7)).isoformat(),
        (date.today() + timedelta(days=14)).isoformat(),
    ]

    import scanner as sc
    import db as database
    import notify as ntfy
    import booker as bkr
    import agent

    inserted_prices: list[str] = []
    trigger_notifications: list[str] = []
    booking_calls: list[str] = []

    fake_flight_factory = lambda origin, destination, cabin, travel_date: sc.Flight(
        origin=origin, destination=destination, cabin=cabin,
        travel_date=travel_date, price_usd=1000.0, source="google_flights",
    )

    with (
        patch.object(sc, "get_price", side_effect=fake_flight_factory),
        patch.object(sc, "build_date_list", return_value=fake_dates),
        patch.object(database, "init_db"),
        patch.object(database, "start_scan_log", return_value=1),
        patch.object(database, "finish_scan_log"),
        patch.object(database, "get_bookings_approaching_deadline", return_value=[]),
        patch.object(database, "insert_price", side_effect=lambda **kw: inserted_prices.append(kw["travel_date"])),
        patch.object(database, "is_already_booked", return_value=False),
        patch.object(ntfy, "price_trigger_found", side_effect=lambda **kw: trigger_notifications.append(kw["travel_date"])),
        patch.object(ntfy, "booking_confirmed"),
        patch.object(ntfy, "scan_summary"),
        patch.object(bkr, "book_flight", side_effect=lambda **kw: (
            booking_calls.append(kw["travel_date"]),
            bkr.BookingResult(success=True, pnr="ABC123", cancel_by="2026-08-01", dry_run=True),
        )[-1]),
        patch.object(database, "insert_booking"),
    ):
        agent.run_scan()

    cfg.ROUTES = original_routes
    cfg.MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN = original_limit
    cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD = original_creds

    # Both dates should have prices recorded
    assert fake_dates[0] in inserted_prices, "First date price not recorded"
    assert fake_dates[1] in inserted_prices, f"Second date price not recorded after booking limit (got {inserted_prices})"

    # Both dates should have triggered a notification
    assert fake_dates[0] in trigger_notifications, "First date trigger notification missing"
    assert fake_dates[1] in trigger_notifications, f"Second date trigger notification missing after booking limit (got {trigger_notifications})"

    # Only one actual booking should have been made (limit = 1)
    assert len(booking_calls) == 1, f"Expected 1 booking call, got {len(booking_calls)}: {booking_calls}"
    assert booking_calls[0] == fake_dates[0], f"Wrong date booked: {booking_calls[0]}"


# ---------------------------------------------------------------------------
# Google Flights tfs cabin encoding
# ---------------------------------------------------------------------------

def test_google_tfs_encodes_cabin_and_route():
    """The tfs protobuf must carry route, date, Delta-only, and the right seat class."""
    import base64

    import scanner
    importlib.reload(scanner)

    tfs = scanner._google_tfs("ATL", "CDG", "2026-08-07", "delta_one")
    raw = base64.urlsafe_b64decode(tfs + "=" * (-len(tfs) % 4))
    assert b"ATL" in raw and b"CDG" in raw and b"2026-08-07" in raw

    # seat field: key (9<<3)|0 = 0x48, value 3 (business) for delta_one
    assert bytes([0x48, 3]) in raw
    # first class encodes seat 4
    raw_first = base64.urlsafe_b64decode(
        (lambda t: t + "=" * (-len(t) % 4))(scanner._google_tfs("ATL", "CDG", "2026-08-07", "first"))
    )
    assert bytes([0x48, 4]) in raw_first

    # airlines field on the leg: key (6<<3)|2 = 0x32, len 2, "DL"
    assert bytes([0x32, 2]) + b"DL" in raw

    # The Google scraper must use the tfs URL, not the old q= form that
    # ignores the cabin filter
    with open("scanner.py") as f:
        src = f.read()
    assert "q=flights+from" not in src, "Old q= URL (ignores cabin filter) still present"
    assert "tfs=" in src


# ---------------------------------------------------------------------------
# Post-scan quarantine
# ---------------------------------------------------------------------------

def test_quarantine_purges_implausible_scan():
    """A scan where >90% of dates trigger must purge its price rows."""
    import agent
    importlib.reload(agent)

    with open("agent.py") as f:
        src = f.read()
    assert "QUARANTINE_TRIGGER_RATE" in src, "Quarantine check missing from agent.py"
    assert "delete_prices_since" in src, "Quarantine purge call missing from agent.py"

    # db helper deletes rows at/after the timestamp
    import tempfile, os as _os

    import config
    import db
    importlib.reload(db)
    with tempfile.TemporaryDirectory() as tmp:
        old_path = config.DB_PATH
        config.DB_PATH = _os.path.join(tmp, "t.db")
        try:
            db.init_db()
            db.insert_price("ATL", "CDG", "delta_one", "2026-08-07", 2000.0, "google_flights")
            deleted = db.delete_prices_since("2000-01-01T00:00:00")
            assert deleted == 1, f"Expected 1 purged row, got {deleted}"
        finally:
            config.DB_PATH = old_path


# ---------------------------------------------------------------------------
# Bonus: build_date_list correctness
# ---------------------------------------------------------------------------

def test_build_date_list_only_fri_sat():
    import scanner
    importlib.reload(scanner)
    dates = scanner.build_date_list()
    assert len(dates) > 0, "build_date_list returned no dates"
    for d_str in dates:
        d = date.fromisoformat(d_str)
        assert d.weekday() in (4, 5), f"{d_str} is {d.strftime('%A')}, not Friday or Saturday"


# ---------------------------------------------------------------------------
# Rolling date window (the fixed Jun–Oct 2026 window expired on Oct 31)
# ---------------------------------------------------------------------------

def test_rolling_window_moves_with_scan_date():
    """Dates are Fri/Sat between +14 and +126 days of the scan date, whatever that date is."""
    from datetime import timedelta

    import config
    import scanner
    importlib.reload(scanner)

    for today in (date(2026, 10, 20), date(2026, 11, 1), date(2027, 6, 15)):
        dates = scanner.build_date_list(today=today)
        lo = today + timedelta(days=config.SCAN_HORIZON_START_DAYS)
        hi = today + timedelta(days=config.SCAN_HORIZON_END_DAYS)
        assert 30 <= len(dates) <= 34, f"{today}: expected ~33 Fri/Sat dates, got {len(dates)}"
        assert date.fromisoformat(dates[0]) >= lo and date.fromisoformat(dates[-1]) <= hi, \
            f"{today}: {dates[0]}..{dates[-1]} outside {lo}..{hi}"
        for d in dates:
            assert date.fromisoformat(d).weekday() in (4, 5), f"{d} is not a Friday or Saturday"

    # The day after the old fixed window ended, there is still a full window ahead
    after_old_end = scanner.build_date_list(today=date(2026, 11, 1))
    assert all(d > "2026-10-31" for d in after_old_end)
    assert scanner.build_date_list(today=date(2026, 10, 20)) != after_old_end, "window did not roll"


def test_date_windows_interface_for_consumers():
    """export_snapshot and app.py read w.start / w.end as ISO strings; fixed
    DateWindows must keep working alongside rolling ones."""
    import json

    import config
    import scanner
    importlib.reload(scanner)

    exported = [{"start": w.start, "end": w.end} for w in config.DATE_WINDOWS]
    json.dumps(exported)
    for w in exported:
        assert date.fromisoformat(w["start"]) <= date.fromisoformat(w["end"])
        assert w["start"] > date.today().isoformat(), "rolling window should start in the future"

    original = config.DATE_WINDOWS
    try:
        config.DATE_WINDOWS = [
            config.DateWindow("2027-03-01", "2027-03-07"),     # Fri 5th, Sat 6th
            config.RollingDateWindow(14, 20),
        ]
        today = date(2027, 2, 15)                              # rolling: Mar 1–7 too
        assert scanner.build_date_list(today=today) == ["2027-03-05", "2027-03-06"], \
            "fixed + overlapping rolling windows should merge without duplicates"
    finally:
        config.DATE_WINDOWS = original


def test_scan_size_within_ci_budget():
    """Each lookup sleeps REQUEST_DELAY_SECONDS (twice on a Kayak fallback) and
    CI has a 30-min timeout. The largest scans proven in CI were 128–144 lookups
    (Jul 2026); don't widen the horizon past that without re-checking run times."""
    import config
    import scanner
    importlib.reload(scanner)

    for today in (date(2026, 10, 20), date(2027, 1, 1), date(2027, 5, 7)):
        lookups = len(scanner.build_date_list(today=today)) * len(config.ROUTES)
        assert 16 < lookups <= 144, f"{today}: {lookups} lookups per scan"


# ---------------------------------------------------------------------------
# Tokyo is Haneda (Delta left Narita in 2020; ATL→NRT never priced)
# ---------------------------------------------------------------------------

def test_tokyo_route_is_haneda():
    import config
    assert not [r for r in config.ROUTES if "NRT" in (r.origin, r.destination)], "Delta doesn't fly ATL–NRT"
    hnd = [r for r in config.ROUTES if (r.origin, r.destination) == ("ATL", "HND")]
    assert len(hnd) == 1, "ATL→HND route missing"
    r = hnd[0]
    assert (r.cabin, r.auto_book) == ("delta_one", True), \
        "cabin and auto_book should carry over from the old ATL→NRT route"


def test_snapshot_omits_unconfigured_routes():
    """Old ATL→NRT rows stay in the DB but must not reach the dashboard snapshot."""
    import os as _os
    import sqlite3
    import tempfile
    from datetime import timedelta

    import config
    import db
    import export_snapshot
    importlib.reload(db)
    importlib.reload(export_snapshot)

    future = (date.today() + timedelta(days=30)).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        old_path = config.DB_PATH
        config.DB_PATH = _os.path.join(tmp, "t.db")
        try:
            db.init_db()
            db.insert_price("ATL", "NRT", "delta_one", future, 2000.0, "google_flights")
            db.insert_price("ATL", "HND", "delta_one", future, 6211.0, "google_flights")
            snap = export_snapshot.export()

            con = sqlite3.connect(config.DB_PATH)
            kept = con.execute("SELECT COUNT(*) FROM price_history WHERE destination='NRT'").fetchone()[0]
            con.close()
            assert kept == 1, "history rows must not be deleted"
        finally:
            config.DB_PATH = old_path

    assert {(p["origin"], p["destination"]) for p in snap["price_history"]} == {("ATL", "HND")}
    assert "ATL|HND" in snap["price_summary"] and "ATL|NRT" not in snap["price_summary"]
    all_routes = snap["price_summary"][""]
    assert [row["min_price"] for row in all_routes] == [6211.0], f"NRT leaked into all-routes summary: {all_routes}"
    configured = {(r["origin"], r["destination"]) for r in snap["config"]["routes"]}
    assert ("ATL", "HND") in configured and ("ATL", "NRT") not in configured
    assert all("start" in w and "end" in w for w in snap["config"]["date_windows"])


# ---------------------------------------------------------------------------
# No Delta login (CI): skip the booking attempt instead of logging an error
# ---------------------------------------------------------------------------

def _scan_with_credentials(username: str, password: str, booker_result=None) -> dict:
    """Run agent.run_scan() over two below-threshold dates on one auto-book
    route with all I/O mocked. Returns what each mock saw."""
    from datetime import timedelta

    import agent
    import booker as bkr
    import config as cfg
    import db as database
    import notify as ntfy
    import scanner as sc

    seen = {"prices": [], "triggers": [], "book_calls": [], "errors": [], "finish": {}}
    dates = [(date.today() + timedelta(days=d)).isoformat() for d in (20, 21)]
    flight = lambda origin, destination, cabin, travel_date: sc.Flight(
        origin=origin, destination=destination, cabin=cabin,
        travel_date=travel_date, price_usd=1000.0, source="google_flights",
    )

    def book(**kw):
        seen["book_calls"].append(kw["travel_date"])
        return booker_result

    saved = (cfg.ROUTES, cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD)
    cfg.ROUTES = [cfg.Route("ATL", "CDG", "delta_one", max_price=9999.0, auto_book=True)]
    cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD = username, password
    try:
        with patch.object(sc, "get_price", side_effect=flight), \
             patch.object(sc, "build_date_list", return_value=dates), \
             patch.object(database, "start_scan_log", return_value=1), \
             patch.object(database, "finish_scan_log", side_effect=lambda **kw: seen["finish"].update(kw)), \
             patch.object(database, "get_bookings_approaching_deadline", return_value=[]), \
             patch.object(database, "get_hours_since_last_price", return_value=None), \
             patch.object(database, "insert_price", side_effect=lambda **kw: seen["prices"].append(kw["travel_date"])), \
             patch.object(database, "is_already_booked", return_value=False) as already_booked, \
             patch.object(database, "insert_booking"), \
             patch.object(ntfy, "price_trigger_found", side_effect=lambda **kw: seen["triggers"].append(kw["travel_date"])), \
             patch.object(ntfy, "booking_confirmed"), \
             patch.object(ntfy, "error_alert", side_effect=lambda **kw: seen["errors"].append(kw)), \
             patch.object(ntfy, "scan_summary"), \
             patch.object(bkr, "book_flight", side_effect=book):
            seen["summary"] = agent.run_scan()
            seen["already_booked_checks"] = already_booked.call_count
    finally:
        cfg.ROUTES, cfg.DELTA_USERNAME, cfg.DELTA_PASSWORD = saved
    seen["dates"] = dates
    return seen


def test_no_credentials_skips_booking_without_error():
    """CI has no Delta login: record the price and fire the trigger, but don't
    call the booker or count an error (it showed as errors=47 on the demo)."""
    for username, password in (("", ""), ("someone", ""), ("", "secret-placeholder")):
        seen = _scan_with_credentials(username, password)
        s = seen["summary"]
        assert seen["book_calls"] == [], f"booker called without credentials: {seen['book_calls']}"
        assert seen["prices"] == seen["dates"], "prices must still be recorded"
        assert seen["triggers"] == seen["dates"], "trigger notifications must still fire"
        assert (s["trigger_count"], s["booking_count"], s["error_count"]) == (2, 0, 0), s
        assert seen["finish"]["error_count"] == 0 and seen["finish"]["booking_count"] == 0
        assert seen["errors"] == [], "no error alert without credentials"
        assert seen["already_booked_checks"] == 2, "is_already_booked guard still runs first"

    with open("agent.py") as f:
        src = f.read()
    assert "no Delta credentials configured" in src
    assert "db.is_already_booked(" in src, "is_already_booked guard must stay"


def test_credentials_set_still_books_as_before():
    """With a Delta login the booker is called exactly as before: one booking
    per route per scan; a failed booking still counts as an error."""
    import booker as bkr

    ok = bkr.BookingResult(success=True, pnr="ABC123", cancel_by="2026-12-01", dry_run=True)
    seen = _scan_with_credentials("placeholder-user", "placeholder-pass", booker_result=ok)
    assert seen["book_calls"] == seen["dates"][:1], f"expected one booking call, got {seen['book_calls']}"
    assert (seen["summary"]["booking_count"], seen["summary"]["error_count"]) == (1, 0)

    failed = bkr.BookingResult(success=False, error="selector changed", dry_run=True)
    seen = _scan_with_credentials("placeholder-user", "placeholder-pass", booker_result=failed)
    assert seen["book_calls"] == seen["dates"], "a failed booking doesn't use up the per-route limit"
    assert (seen["summary"]["booking_count"], seen["summary"]["error_count"]) == (0, 2)
    assert len(seen["errors"]) == 2


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

TESTS = [
    test_booker_has_passenger_logic,
    test_booker_has_ecredit_balance_check,
    test_ecredit_balance_parse_logic,
    test_scan_route_removed,
    test_dead_url_removed,
    test_no_redundant_datetime_import,
    test_cancel_by_parsing_uses_module_datetime,
    test_google_json_price_bounds,
    test_google_flights_rejects_low_price,
    test_google_flights_accepts_valid_delta_price,
    test_google_flights_counts_only_delta_single_cabin,
    test_scan_summary_odd_condition_removed,
    test_scan_summary_no_crash_without_pushover,
    test_agent_uses_continue_not_break,
    test_price_history_recorded_past_booking_limit,
    test_google_tfs_encodes_cabin_and_route,
    test_quarantine_purges_implausible_scan,
    test_build_date_list_only_fri_sat,
    test_rolling_window_moves_with_scan_date,
    test_date_windows_interface_for_consumers,
    test_scan_size_within_ci_budget,
    test_tokyo_route_is_haneda,
    test_snapshot_omits_unconfigured_routes,
    test_no_credentials_skips_booking_without_error,
    test_credentials_set_still_books_as_before,
]

if __name__ == "__main__":
    sys.path.insert(0, ".")
    print(f"\nRunning {len(TESTS)} tests...\n")
    for t in TESTS:
        _run(t)
    total = len(_PASS) + len(_FAIL)
    print(f"\n{'='*50}")
    print(f"Results: {len(_PASS)}/{total} passed")
    if _FAIL:
        print("\nFailed tests:")
        for name, exc in _FAIL:
            print(f"  {name}: {exc}")
    sys.exit(0 if not _FAIL else 1)
