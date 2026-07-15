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
    data_json = json.dumps([[900]])
    fake_html = f'AF_initDataCallback({{key: "x", data: {data_json} }});'
    assert scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04") is None


def test_collect_prices_range():
    import scanner
    importlib.reload(scanner)

    prices: list[float] = []
    scanner._collect_prices([50, 99, 100, 500, 19_999, 20_000, 50_000, 50_001], prices)
    assert 500.0 in prices
    assert 100.0 in prices  # _collect_prices lower bound is 100
    assert 99.0 not in prices
    assert 50_001.0 not in prices


def test_google_flights_json_path_rejects_low_price():
    """_parse_google_price should reject a price of 200 (below 500 threshold)."""
    import scanner
    importlib.reload(scanner)
    import json, re

    # Craft a fake AF_initDataCallback block that embeds a price of 200
    price_val = 200
    data_json = json.dumps([[price_val]])
    fake_html = f'AF_initDataCallback({{key: "x", data: {data_json} }});'
    result = scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04")
    assert result is None, f"Expected None for sub-500 price, got {result}"


def test_google_flights_json_path_accepts_valid_price():
    """_parse_google_price should accept a price of 3000."""
    import scanner
    importlib.reload(scanner)
    import json

    price_val = 3000
    data_json = json.dumps([[price_val]])
    fake_html = f'AF_initDataCallback({{key: "x", data: {data_json} }});'
    result = scanner._parse_google_price(fake_html, "ATL", "CDG", "delta_one", "2026-07-04")
    assert result is not None, "Expected a Flight for valid price 3000"
    assert result.price_usd == 3000.0


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
    """The tfs protobuf must carry route, date, and the right seat class."""
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
    test_collect_prices_range,
    test_google_flights_json_path_rejects_low_price,
    test_google_flights_json_path_accepts_valid_price,
    test_scan_summary_odd_condition_removed,
    test_scan_summary_no_crash_without_pushover,
    test_agent_uses_continue_not_break,
    test_price_history_recorded_past_booking_limit,
    test_google_tfs_encodes_cabin_and_route,
    test_quarantine_purges_implausible_scan,
    test_build_date_list_only_fri_sat,
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
