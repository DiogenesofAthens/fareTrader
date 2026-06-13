"""
fareTrader web UI — FastAPI backend.

Start with:  uvicorn app:app --reload --port 8000
Then open:   http://localhost:8000

On Vercel (VERCEL=1), only the static dashboard is served.
The live API endpoints require the local SQLite backend.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

_ON_VERCEL = bool(os.getenv("VERCEL"))

app = FastAPI(title="fareTrader", docs_url=None if _ON_VERCEL else "/api/docs")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Static dashboard — works on Vercel and locally
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/robots.txt")
def robots():
    return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap():
    return FileResponse(STATIC_DIR / "sitemap.xml", media_type="application/xml")


@app.get("/llms.txt")
def llms():
    return FileResponse(STATIC_DIR / "llms.txt", media_type="text/plain")


# ---------------------------------------------------------------------------
# Live API — only registered when not on Vercel (requires local SQLite)
# ---------------------------------------------------------------------------

if not _ON_VERCEL:
    from contextlib import contextmanager
    from datetime import datetime
    from typing import Generator, Optional
    import sqlite3
    from fastapi import BackgroundTasks, HTTPException
    from fastapi.staticfiles import StaticFiles
    import config
    import db

    db.init_db()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    _scan_state: dict = {"running": False, "last_result": None, "last_run": None}

    @contextmanager
    def _db() -> Generator[sqlite3.Connection, None, None]:
        con = sqlite3.connect(config.DB_PATH)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
        finally:
            con.close()

    @app.get("/api/stats")
    def stats():
        with _db() as con:
            held = con.execute("SELECT COUNT(*) FROM bookings WHERE status='held'").fetchone()[0]
            prices = con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            last_scan = con.execute(
                "SELECT started_at, routes_checked, dates_checked, trigger_count, "
                "booking_count, error_count, dry_run FROM scan_log ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            total_triggers = con.execute(
                "SELECT COALESCE(SUM(trigger_count), 0) FROM scan_log"
            ).fetchone()[0]
            total_scans = con.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0]
        return {
            "routes_monitored": len(config.ROUTES),
            "held_bookings": int(held),
            "total_price_points": int(prices),
            "total_triggers": int(total_triggers),
            "total_scans": int(total_scans),
            "last_scan": dict(last_scan) if last_scan else None,
            "dry_run_mode": config.DRY_RUN,
            "scan_interval_minutes": config.SCAN_INTERVAL_MINUTES,
            "scan_running": _scan_state["running"],
        }

    @app.get("/api/bookings")
    def bookings(status: Optional[str] = None):
        q = "SELECT * FROM bookings"
        p: list = []
        if status:
            q += " WHERE status=?"
            p.append(status)
        q += " ORDER BY travel_date"
        with _db() as con:
            rows = con.execute(q, p).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/bookings/{pnr}/cancel")
    def cancel_booking(pnr: str):
        if not db.cancel_booking(pnr.upper()):
            raise HTTPException(404, f"PNR {pnr} not found among held bookings")
        return {"ok": True}

    @app.get("/api/price-history")
    def price_history(
        origin: Optional[str] = None,
        destination: Optional[str] = None,
        limit: int = 100,
    ):
        conds: list[str] = []
        params: list = []
        if origin:
            conds.append("origin=?")
            params.append(origin.upper())
        if destination:
            conds.append("destination=?")
            params.append(destination.upper())
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        params.append(limit)
        with _db() as con:
            rows = con.execute(
                f"SELECT * FROM price_history {where} ORDER BY scanned_at DESC LIMIT ?", params
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/price-summary")
    def price_summary(
        origin: Optional[str] = None,
        destination: Optional[str] = None,
    ):
        """Cheapest price per upcoming travel_date — drives the bar chart."""
        conds = ["travel_date >= date('now')"]
        params: list = []
        if origin:
            conds.append("origin=?")
            params.append(origin.upper())
        if destination:
            conds.append("destination=?")
            params.append(destination.upper())
        where = " AND ".join(conds)
        with _db() as con:
            rows = con.execute(
                f"""SELECT travel_date,
                           MIN(price_usd)        AS min_price,
                           MAX(price_usd)        AS max_price,
                           ROUND(AVG(price_usd)) AS avg_price,
                           COUNT(*)              AS observations
                    FROM price_history WHERE {where}
                    GROUP BY travel_date ORDER BY travel_date""",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/scan-log")
    def scan_log(limit: int = 30):
        with _db() as con:
            rows = con.execute(
                "SELECT * FROM scan_log ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/config")
    def get_config():
        return {
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
        }

    @app.get("/api/scan/status")
    def scan_status():
        return _scan_state

    @app.post("/api/scan")
    def trigger_scan(background_tasks: BackgroundTasks):
        if _scan_state["running"]:
            raise HTTPException(409, "A scan is already in progress")
        background_tasks.add_task(_run_scan_task)
        return {"ok": True}

    def _run_scan_task() -> None:
        from agent import run_scan
        _scan_state["running"] = True
        _scan_state["last_result"] = None
        try:
            result = run_scan()
            _scan_state["last_result"] = result
        except Exception as exc:
            _scan_state["last_result"] = {"error": str(exc)}
        finally:
            _scan_state["last_run"] = datetime.utcnow().isoformat()
            _scan_state["running"] = False


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
