"""
Price scanner: Google Flights first, Kayak as fallback.
Returns structured Flight objects. Never raises — returns empty list on failure.
"""
from __future__ import annotations

import json
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


def build_date_list() -> list[str]:
    """
    Return sorted list of YYYY-MM-DD strings that are Fridays or Saturdays
    within any configured DateWindow.
    """
    dates: set[str] = set()
    for window in config.DATE_WINDOWS:
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

# Google Flights embeds flight data as JSON inside the page HTML.
# The pattern below targets the AF_initDataCallback blocks that carry pricing.
_GF_JSON_PATTERN = re.compile(
    r"AF_initDataCallback\(\{[^}]*?data:([\s\S]*?)\}\s*\);",
    re.MULTILINE,
)

# Price pattern for fallback plain-text extraction
_PRICE_PATTERN = re.compile(r"\$\s*(\d[\d,]+)")


def _cabin_code_google(cabin: str) -> str:
    """Map our cabin name to Google Flights cabin filter."""
    mapping = {
        "delta_one": "c",    # Business / Delta One
        "business":  "c",
        "first":     "f",
    }
    return mapping.get(cabin, "c")


# Cheapest believable fare per cabin. Google/Kayak sometimes ignore the cabin
# filter and surface default-cabin (economy) prices — e.g. $540–$1,794 for
# transatlantic "Delta One" observed from CI. Anything below the floor is
# treated as not-our-cabin and discarded; recording nothing beats recording
# a wrong-cabin price that fires a false trigger.
_MIN_PLAUSIBLE_BY_CABIN = {"delta_one": 1500.0, "business": 1500.0, "first": 800.0}


def _min_plausible(cabin: str) -> float:
    return _MIN_PLAUSIBLE_BY_CABIN.get(cabin, 500.0)


def _parse_google_price(html: str, origin: str, destination: str, cabin: str, travel_date: str) -> Optional[Flight]:
    """
    Parse a price from Google Flights HTML. Google embeds structured data
    in AF_initDataCallback JSON blobs; we do a best-effort extraction.
    Falls back to the cheapest price found via regex in the HTML.
    """
    # Attempt structured JSON extraction first
    for match in _GF_JSON_PATTERN.finditer(html):
        raw = match.group(1)
        try:
            data = json.loads(raw)
            # Walk the nested list structure looking for numeric prices
            prices: list[float] = []
            _collect_prices(data, prices)
            prices = [p for p in prices if p >= _min_plausible(cabin)]
            if prices:
                price = min(prices)
                if price <= 20_000:
                    return Flight(
                        origin=origin,
                        destination=destination,
                        cabin=cabin,
                        travel_date=travel_date,
                        price_usd=price,
                        source="google_flights",
                    )
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    # Fallback: regex scan for dollar amounts in the HTML
    prices_found = [float(m.group(1).replace(",", "")) for m in _PRICE_PATTERN.finditer(html)]
    # Filter to plausible premium-cabin range
    plausible = [p for p in prices_found if _min_plausible(cabin) <= p <= 20_000]
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


def _collect_prices(obj: object, acc: list[float], depth: int = 0) -> None:
    """Recursively walk nested lists/dicts collecting integer-like price values."""
    if depth > 12:
        return
    if isinstance(obj, (int, float)) and 100 <= obj <= 50_000:
        acc.append(float(obj))
    elif isinstance(obj, list):
        for item in obj:
            _collect_prices(item, acc, depth + 1)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_prices(v, acc, depth + 1)


def _scrape_google_flights(
    session: requests.Session,
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
) -> Optional[Flight]:
    cabin_code = _cabin_code_google(cabin)
    date_obj = date.fromisoformat(travel_date)
    date_str = date_obj.strftime("%Y-%m-%d")
    url = (
        f"https://www.google.com/travel/flights?"
        f"q=flights+from+{origin}+to+{destination}+on+{date_str}"
        f"&cabin={cabin_code}&curr=USD&hl=en"
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

        # Kayak embeds prices in elements with data-price or specific class names.
        # Class names change; we look for the pattern in multiple ways.
        price: Optional[float] = None

        # Method 1: data-price attributes
        for tag in soup.find_all(attrs={"data-price": True}):
            try:
                val = float(str(tag["data-price"]).replace(",", ""))
                if _min_plausible(cabin) <= val <= 25_000:
                    price = val if price is None else min(price, val)
            except (ValueError, TypeError):
                pass

        # Method 2: JSON blobs in <script> tags
        for script in soup.find_all("script", type="application/json"):
            try:
                data = json.loads(script.string or "")
                candidates: list[float] = []
                _collect_prices(data, candidates)
                plausible = [p for p in candidates if _min_plausible(cabin) <= p <= 25_000]
                if plausible:
                    best = min(plausible)
                    price = best if price is None else min(price, best)
            except (json.JSONDecodeError, TypeError):
                pass

        # Method 3: dollar regex fallback
        if price is None:
            matches = [float(m.group(1).replace(",", "")) for m in _PRICE_PATTERN.finditer(resp.text)]
            plausible = [p for p in matches if _min_plausible(cabin) <= p <= 25_000]
            if plausible:
                price = min(plausible)

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
