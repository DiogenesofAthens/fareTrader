"""
Central configuration. All secrets come from environment variables.
Edit ROUTES and DATE_WINDOWS directly; everything else via env vars.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Route definitions
# ---------------------------------------------------------------------------

@dataclass
class Route:
    origin: str
    destination: str
    cabin: str          # "first" | "business" | "delta_one"
    max_price: float    # USD threshold — book at or below this
    auto_book: bool     # False → alert only, never purchase
    seats: int = 1      # seats per booking attempt

ROUTES: list[Route] = [
    Route("ATL", "CDG", "delta_one", 5000.0, auto_book=True),
    Route("ATL", "NRT", "delta_one", 6500.0, auto_book=True),
    Route("ATL", "LHR", "delta_one", 4000.0, auto_book=False),  # alert only
    Route("JFK", "MXP", "delta_one", 4200.0, auto_book=True),
]

# ---------------------------------------------------------------------------
# Date windows — agent scans Fridays & Saturdays within each window
# ---------------------------------------------------------------------------

@dataclass
class DateWindow:
    start: str   # YYYY-MM-DD inclusive
    end: str     # YYYY-MM-DD inclusive

DATE_WINDOWS: list[DateWindow] = [
    DateWindow("2026-06-01", "2026-10-31"),
]

# ---------------------------------------------------------------------------
# Operational settings
# ---------------------------------------------------------------------------

SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "120"))

# Never book more than this many new dates per route per scan run
MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN: int = 1

# Keep this many eCredits in reserve (USD) — don't book if balance would drop below
MIN_ECREDIT_BUFFER_USD: float = float(os.getenv("MIN_ECREDIT_BUFFER_USD", "500.0"))

# DRY_RUN=True (default) runs the full Playwright flow but skips the final
# purchase confirmation click. Set DRY_RUN=False in env to go live.
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() not in ("false", "0", "no")

# Polite delay between outbound scrape requests (seconds)
REQUEST_DELAY_SECONDS: float = 3.0

# ---------------------------------------------------------------------------
# Credentials (never hardcode — always from env)
# ---------------------------------------------------------------------------

DELTA_USERNAME: str = os.getenv("DELTA_USERNAME", "")
DELTA_PASSWORD: str = os.getenv("DELTA_PASSWORD", "")

PUSHOVER_USER_KEY: str = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN: str = os.getenv("PUSHOVER_APP_TOKEN", "")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_PATH: str = os.getenv("DB_PATH", "bookings.db")
SCREENSHOT_DIR: str = os.getenv("SCREENSHOT_DIR", ".")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
