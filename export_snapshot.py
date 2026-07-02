"""Export a JSON snapshot of the fareTrader database for the static dashboard.

Writes static/data.json with the same shapes the dashboard's API endpoints
return, so the Vercel deployment can show real scan data instead of canned
demo numbers.

Usage:
    python3 export_snapshot.py           # export current DB contents
    python3 export_snapshot.py --scan    # run a dry-run scan first, then export

Intended to run on a GitHub Actions cron (see .github/workflows/fare-scan.yml),
which commits the refreshed snapshot (and bookings.db, so history accumulates
across runs).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import config

SNAPSHOT_PATH = Path(__file__).parent / "static" / "data.json"

PRICE_HISTORY_LIMIT = 500
SCAN_LOG_LIMIT = 30


def _rows(con: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in con.execute(query, params).fetchall()]


def _savings_captured(con: sqlite3.Connection) -> float:
    """Sum over held bookings of (max observed fare for the same slot − booked price).

    Every held seat was bought at or below threshold; the spread against the
    highest fare observed for that route/date is the value the agent captured.
    """
    rows = con.execute(
        """SELECT b.price_usd AS booked,
                  (SELECT MAX(p.price_usd) FROM price_history p
                    WHERE p.origin = b.origin AND p.destination = b.destination
                      AND p.cabin = b.cabin AND p.travel_date = b.travel_date) AS max_seen
             FROM bookings b WHERE b.status = 'held'"""
    ).fetchall()
    total = 0.0
    for booked, max_seen in rows:
        if max_seen is not None and max_seen > booked:
            total += max_seen - booked
    return round(total)


def _price_summary(con: sqlite3.Connection, origin: str | None, destination: str | None) -> list[dict]:
    conds = ["travel_date >= date('now')"]
    params: list = []
    if origin:
        conds.append("origin=?")
        params.append(origin)
    if destination:
        conds.append("destination=?")
        params.append(destination)
    where = " AND ".join(conds)
    return _rows(
        con,
        f"""SELECT travel_date,
                   MIN(price_usd)        AS min_price,
                   MAX(price_usd)        AS max_price,
                   ROUND(AVG(price_usd)) AS avg_price,
                   COUNT(*)              AS observations
              FROM price_history WHERE {where}
              GROUP BY travel_date ORDER BY travel_date""",
        tuple(params),
    )


def export() -> dict:
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row

    held = con.execute("SELECT COUNT(*) FROM bookings WHERE status='held'").fetchone()[0]
    prices = con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    last_scan = con.execute(
        "SELECT started_at, routes_checked, dates_checked, trigger_count, "
        "booking_count, error_count, dry_run FROM scan_log ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    total_triggers = con.execute("SELECT COALESCE(SUM(trigger_count), 0) FROM scan_log").fetchone()[0]
    total_scans = con.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "routes_monitored": len(config.ROUTES),
            "held_bookings": int(held),
            "total_price_points": int(prices),
            "total_triggers": int(total_triggers),
            "total_scans": int(total_scans),
            "last_scan": dict(last_scan) if last_scan else None,
            "dry_run_mode": config.DRY_RUN,
            "scan_interval_minutes": config.SCAN_INTERVAL_MINUTES,
            "scan_running": False,
            "savings_captured_usd": _savings_captured(con),
        },
        "config": {
            "routes": [
                {
                    "origin": r.origin,
                    "destination": r.destination,
                    "cabin": r.cabin,
                    "max_price": r.max_price,
                    "auto_book": r.auto_book,
                    "seats": r.seats,
                }
                for r in config.ROUTES
            ],
            "date_windows": [{"start": w.start, "end": w.end} for w in config.DATE_WINDOWS],
            "dry_run": config.DRY_RUN,
            "scan_interval_minutes": config.SCAN_INTERVAL_MINUTES,
            "min_ecredit_buffer_usd": config.MIN_ECREDIT_BUFFER_USD,
        },
        "bookings": _rows(con, "SELECT * FROM bookings ORDER BY travel_date"),
        "price_history": _rows(
            con,
            "SELECT * FROM price_history ORDER BY scanned_at DESC LIMIT ?",
            (PRICE_HISTORY_LIMIT,),
        ),
        "scan_log": _rows(
            con,
            "SELECT * FROM scan_log ORDER BY started_at DESC LIMIT ?",
            (SCAN_LOG_LIMIT,),
        ),
    }

    # Per-route summaries keyed "ORIGIN|DEST", plus "" for all routes
    summaries = {"": _price_summary(con, None, None)}
    for r in config.ROUTES:
        summaries[f"{r.origin}|{r.destination}"] = _price_summary(con, r.origin, r.destination)
    snapshot["price_summary"] = summaries

    con.close()
    return snapshot


def main() -> None:
    if "--scan" in sys.argv:
        if not config.DRY_RUN:
            print("Refusing to run: --scan requires DRY_RUN=true (never book from CI).")
            sys.exit(1)
        import db
        from agent import run_scan

        db.init_db()
        print("Running dry-run scan…")
        try:
            result = run_scan()
            print(f"Scan finished: {result}")
        except Exception as exc:
            # Still export whatever data exists — a failed scan shouldn't
            # wipe the dashboard.
            print(f"Scan failed ({exc}); exporting existing data anyway.")

    snapshot = export()
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=1))
    print(
        f"Wrote {SNAPSHOT_PATH} — {snapshot['stats']['total_price_points']} price points, "
        f"{len(snapshot['bookings'])} bookings, savings captured "
        f"${snapshot['stats']['savings_captured_usd']:,}"
    )


if __name__ == "__main__":
    main()
