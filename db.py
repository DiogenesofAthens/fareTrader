"""
SQLite persistence layer. Auto-creates schema on first import.
All writes go through explicit functions; callers never touch raw SQL.
"""
from __future__ import annotations

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Generator, Optional

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pnr         TEXT    NOT NULL UNIQUE,
    origin      TEXT    NOT NULL,
    destination TEXT    NOT NULL,
    cabin       TEXT    NOT NULL,
    travel_date TEXT    NOT NULL,   -- YYYY-MM-DD
    seats       INTEGER NOT NULL DEFAULT 1,
    price_usd   REAL    NOT NULL,
    cancel_by   TEXT,               -- YYYY-MM-DD, populated from Delta policy
    status      TEXT    NOT NULL DEFAULT 'held', -- held | cancelled | flown
    booked_at   TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    origin      TEXT    NOT NULL,
    destination TEXT    NOT NULL,
    cabin       TEXT    NOT NULL,
    travel_date TEXT    NOT NULL,
    price_usd   REAL    NOT NULL,
    source      TEXT    NOT NULL,   -- google_flights | kayak | unknown
    scanned_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    routes_checked  INTEGER NOT NULL DEFAULT 0,
    dates_checked   INTEGER NOT NULL DEFAULT 0,
    trigger_count   INTEGER NOT NULL DEFAULT 0,
    booking_count   INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    dry_run         INTEGER NOT NULL DEFAULT 1
);
"""

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)
    log.debug("Database ready at %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# Bookings
# ---------------------------------------------------------------------------

def is_already_booked(origin: str, destination: str, cabin: str, travel_date: str) -> bool:
    """Return True if a *held* booking already exists for this slot."""
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM bookings WHERE origin=? AND destination=? AND cabin=? "
            "AND travel_date=? AND status='held'",
            (origin, destination, cabin, travel_date),
        ).fetchone()
    return row is not None


def insert_booking(
    pnr: str,
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    seats: int,
    price_usd: float,
    cancel_by: Optional[str] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO bookings
               (pnr,origin,destination,cabin,travel_date,seats,price_usd,cancel_by,status,booked_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,'held',?,?)""",
            (pnr, origin, destination, cabin, travel_date, seats, price_usd, cancel_by, now, now),
        )
        return cur.lastrowid  # type: ignore[return-value]


def cancel_booking(pnr: str) -> bool:
    """Mark a booking cancelled in the DB. Caller must also cancel on delta.com."""
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        cur = con.execute(
            "UPDATE bookings SET status='cancelled', updated_at=? WHERE pnr=? AND status='held'",
            (now, pnr),
        )
        return cur.rowcount > 0


def get_held_bookings() -> list[sqlite3.Row]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM bookings WHERE status='held' ORDER BY travel_date"
        ).fetchall()
    return rows


def get_bookings_approaching_deadline(days: int = 7) -> list[sqlite3.Row]:
    """Return held bookings whose cancel_by is within *days* days from now."""
    with _conn() as con:
        rows = con.execute(
            """SELECT * FROM bookings
               WHERE status='held'
                 AND cancel_by IS NOT NULL
                 AND date(cancel_by) <= date('now', ? || ' days')
               ORDER BY cancel_by""",
            (str(days),),
        ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def insert_price(
    origin: str,
    destination: str,
    cabin: str,
    travel_date: str,
    price_usd: float,
    source: str,
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO price_history
               (origin,destination,cabin,travel_date,price_usd,source,scanned_at)
               VALUES (?,?,?,?,?,?,?)""",
            (origin, destination, cabin, travel_date, price_usd, source, now),
        )


def delete_prices_since(iso_timestamp: str) -> int:
    """Delete price rows recorded at/after the given UTC ISO timestamp.

    Used by the post-scan quarantine: when nearly every date "triggers",
    the scraper was recording wrong-cabin prices and the whole batch is
    discarded rather than polluting the history.
    """
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM price_history WHERE scanned_at >= ?", (iso_timestamp,)
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Scan log
# ---------------------------------------------------------------------------

def start_scan_log(dry_run: bool) -> int:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO scan_log (started_at, dry_run) VALUES (?, ?)",
            (now, int(dry_run)),
        )
        return cur.lastrowid  # type: ignore[return-value]


def get_hours_since_last_price() -> float | None:
    """
    Return how many hours have elapsed since the most recent price_history row
    was recorded, or None if no price records exist yet.
    """
    with _conn() as con:
        row = con.execute(
            "SELECT MAX(scanned_at) AS last_at FROM price_history"
        ).fetchone()
    if not row or not row["last_at"]:
        return None
    last_at = datetime.fromisoformat(row["last_at"])
    elapsed = (datetime.utcnow() - last_at).total_seconds() / 3600
    return elapsed


def finish_scan_log(
    scan_id: int,
    routes_checked: int,
    dates_checked: int,
    trigger_count: int,
    booking_count: int,
    error_count: int,
) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as con:
        con.execute(
            """UPDATE scan_log SET
               finished_at=?, routes_checked=?, dates_checked=?,
               trigger_count=?, booking_count=?, error_count=?
               WHERE id=?""",
            (now, routes_checked, dates_checked, trigger_count, booking_count, error_count, scan_id),
        )
