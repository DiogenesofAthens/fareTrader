"""
Price scanner: Google Flights first, Kayak as fallback.
Returns structured Flight objects. Never raises — returns empty list on failure.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Flight:
    origin: str
    destination: str
    cabin: str
    travel_date: str        # YYYY-MM-DD
    price_usd: float
    source: str             # google_flights | kayak | unknown
    airline: str = "DL"
    available: bool = True


# ---------------------------------------------------------------------------
# Date utilities
# ---------------------------------------------------------------------------

_WEEKDAY_FRIDAY   = 4
_WEEKDAY_SATURDAY = 5


def build_date_list(today: Optional[date] = None) -> list[str]:
    """
    Return sorted list of YYYY-MM-DD strings that are Fridays or Saturdays
    within any configured window. Rolling windows are measured from *today*
    (default: the real date).
    """
    dates: set[str] = set()
    for configured in config.DATE_WINDOWS:
        window = configured.resolve(today)
        start = date.fromisoformat(window.start)
        end   = date.fromisoformat(window.end)
        current = start
        while current <= end:
            if current.weekday() in (_WEEKDAY_FRIDAY, _WEEKDAY_SATURDAY):
                dates.add(current.isoformat())
            current += timedelta(days=1)
    return sorted(dates)


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


# ---------------------------------------------------------------------------
# Google Flights scraper
# ---------------------------------------------------------------------------

# Google Flights renders one aria-label per itinerary, e.g.
#   "From 9334 US dollars. Nonstop flight with Delta. Leaves ..."
# The label names the carriers and, for mixed-cabin itineraries, the cabins
# ("Premium Economy + Business Class Layover ..."). It is the only place on the
# page that ties a price to an airline and cabin, so it is the only source we
# trust. Bare numbers in the AF_initDataCallback JSON and "$" amounts in the
# HTML can't be attributed and were recording other airlines' fares.
_GF_ITINERARY_PATTERN = re.compile(r'aria-label="From (\d+) US dollars\. ([^"]*)"')
_GF_CARRIER_PATTERN = re.compile(r"flight with ([^.]+)\.")
_GF_OPERATED_BY_PATTERN = re.compile(r"Operated by ([^.]+)\.")
_GF_MIXED_CABIN_PATTERN = re.compile(r"\b(?:Economy|Business(?: Class)?|First(?: Class)?)\s*\+")


def _cabin_code_google(cabin: str) -> str:
    """Map our cabin name to Google Flights cabin filter."""
    mapping = {
        "delta_one": "c",    # Business / Delta One
        "business":  "c",
        "first":     "f",
    }
    return mapping.get(cabin, "c")


# ---------------------------------------------------------------------------
# Google Flights tfs parameter (protobuf)
#
# The q= natural-language URL ignores the &cabin= filter, so Google serves
# default-cabin (economy) prices — which is how CI recorded $600 "Delta One"
# fares. The search page's real filter state lives in the tfs query param:
# a base64url-encoded protobuf. Verified empirically (ATL→CDG 2026-08-07):
# seat=1 returned $428–$894, seat=3 returned $1,958–$9,234.
#
# The seat filter alone still returns every airline's business fares, and
# mixed-cabin itineraries on top. From Aug to Oct 2026 nearly every scan
# triggered on fares like a $3,868 JetBlue "Premium Economy + Business"
# routing while Delta's own ATL→CDG nonstop was $9,334. The leg's airlines
# field (6) restricts results to DL-marketed flights. Verified empirically
# (ATL→CDG 2026-10-30): only Delta and Air France codeshares came back.
# ---------------------------------------------------------------------------

_TFS_SEAT = {"delta_one": 3, "business": 3, "first": 4}  # 1=eco 2=prem-eco 3=biz 4=first


def _pb_varint(n: int) -> bytes:
    out = b""
    while True:
        b7 = n & 0x7F
        n >>= 7
        out += bytes([b7 | (0x80 if n else 0)])
        if not n:
            return out


def _pb_field(num: int, wire: int, payload: bytes) -> bytes:
    key = _pb_varint((num << 3) | wire)
    if wire == 2:  # length-delimited
        return key + _pb_varint(len(payload)) + payload
    return key + payload  # wire 0: payload is already varint-encoded


def _google_tfs(origin: str, destination: str, travel_date: str, cabin: str) -> str:
    """Build the tfs protobuf for a one-way, one-adult, Delta-only, cabin-filtered search."""
    airport_from = _pb_field(2, 2, origin.encode())
    airport_to = _pb_field(2, 2, destination.encode())
    flight_data = (
        _pb_field(2, 2, travel_date.encode())
        + _pb_field(6, 2, b"DL")                           # airlines: Delta only
        + _pb_field(13, 2, airport_from)
        + _pb_field(14, 2, airport_to)
    )
    info = (
        _pb_field(3, 2, flight_data)                       # flight leg
        + _pb_field(8, 0, _pb_varint(1))                   # passengers: 1 adult
        + _pb_field(9, 0, _pb_varint(_TFS_SEAT.get(cabin, 3)))  # seat class
        + _pb_field(19, 0, _pb_varint(2))                  # trip: one-way
    )
    return base64.urlsafe_b64encode(info).decode().rstrip("=")


# Cheapest believable fare per cabin. Google/Kayak sometimes ignore the cabin
# filter and surface default-cabin (economy) prices — e.g. $540–$1,794 for
# transatlantic "Delta One" observed from CI. Anything below the floor is
# treated as not-our-cabin and discarded; recording nothing beats recording
# a wrong-cabin price that fires a false trigger.
_MIN_PLAUSIBLE_BY_CABIN = {"delta_one": 1500.0, "business": 1500.0, "first": 800.0}


def _min_plausible(cabin: str) -> float:
    return _MIN_PLAUSIBLE_BY_CABIN.get(cabin, 500.0)


def _is_delta_single_cabin(label: str) -> bool:
    """True if an itinerary label is Delta-marketed, Delta-operated and one cabin throughout."""
    carrier = _GF_CARRIER_PATTERN.search(label)
    if not carrier or carrier.group(1).strip() != "Delta":
        return False
    operated_by = _GF_OPERATED_BY_PATTERN.search(label)
    if operated_by and "Delta" not in operated_by.group(1):
        return False
    return not _GF_MIXED_CABIN_PATTERN.search(label)


def _parse_google_price(html: str, origin: str, destination: str, cabin: str, travel_date: str) -> Optional[Flight]:
    """
    Parse the cheapest Delta fare in our cabin from Google Flights HTML.
    Only itineraries whose label says Delta, operated by Delta, with no
    mixed-cabin segments count. Returns None rather than guess.
    """
    prices = [
        float(m.group(1))
        for m in _GF_ITINERARY_PATTERN.finditer(html)
        if _is_delta_single_cabin(m.group(2))
    ]
    plausible = [p for p in prices if _min_plausible(cabin) <= p <= 20_000]
    if plausible:
        return Flight(
            origin=origin,
            destination=destination,
            cabin=cabin,
            travel_date=travel_date,
            price_usd=min(plausible),
            source="google_flights",
        )
    return None


def _scrape_google_flights(
    session: requests.Session,
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
) -> Optional[Flight]:
    date_obj = date.fromisoformat(travel_date)
    date_str = date_obj.strftime("%Y-%m-%d")
    url = (
        f"https://www.google.com/travel/flights/search?"
        f"tfs={_google_tfs(origin, destination, date_str, cabin)}&hl=en&curr=USD"
    )
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        flight = _parse_google_price(resp.text, origin, destination, cabin, travel_date)
        if flight:
            log.debug("Google Flights: %s→%s %s $%.0f", origin, destination, travel_date, flight.price_usd)
        return flight
    except requests.RequestException as exc:
        log.warning("Google Flights request failed (%s→%s %s): %s", origin, destination, travel_date, exc)
        return None


# ---------------------------------------------------------------------------
# Kayak scraper (fallback)
# ---------------------------------------------------------------------------

def _scrape_kayak(
    session: requests.Session,
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
) -> Optional[Flight]:
    cabin_map = {"delta_one": "b", "business": "b", "first": "f"}
    cabin_code = cabin_map.get(cabin, "b")
    date_obj = date.fromisoformat(travel_date)
    date_fmt = date_obj.strftime("%Y-%m-%d")
    url = (
        f"https://www.kayak.com/flights/{origin}-{destination}/{date_fmt}"
        f"?cabin={cabin_code}&airlines=DL&sort=price_a&fs=airlines=DL"
    )
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Kayak renders results client-side, so the HTML we fetch usually has
        # no fares at all. Only data-price attributes on result elements count.
        # The old JSON-walk and "$" regex fallbacks picked up page constants
        # (a flat $2,000 on most dates in Oct 2026), so they were removed.
        price: Optional[float] = None

        for tag in soup.find_all(attrs={"data-price": True}):
            try:
                val = float(str(tag["data-price"]).replace(",", ""))
                if _min_plausible(cabin) <= val <= 25_000:
                    price = val if price is None else min(price, val)
            except (ValueError, TypeError):
                pass

        if price:
            log.debug("Kayak: %s→%s %s $%.0f", origin, destination, travel_date, price)
            return Flight(
                origin=origin,
                destination=destination,
                cabin=cabin,
                travel_date=travel_date,
                price_usd=price,
                source="kayak",
            )
        return None
    except requests.RequestException as exc:
        log.warning("Kayak request failed (%s→%s %s): %s", origin, destination, travel_date, exc)
        return None


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_price(origin: str, destination: str, cabin: str, travel_date: str) -> Optional[Flight]:
    """
    Try Google Flights; fall back to Kayak. Returns None if both fail.
    Applies the configured polite delay after each request.
    """
    session = _session()

    flight = _scrape_google_flights(session, origin, destination, cabin, travel_date)
    time.sleep(config.REQUEST_DELAY_SECONDS)

    if flight is None:
        log.info("Google Flights missed — trying Kayak for %s→%s %s", origin, destination, travel_date)
        flight = _scrape_kayak(session, origin, destination, cabin, travel_date)
        time.sleep(config.REQUEST_DELAY_SECONDS)

    if flight is None:
        log.warning("No price found for %s→%s %s", origin, destination, travel_date)

    return flight
