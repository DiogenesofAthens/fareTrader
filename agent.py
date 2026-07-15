"""
fareTrader — main orchestrator, scheduler, and CLI.

Usage:
    python agent.py           # run on schedule (default every 2 hours)
    python agent.py --once    # single scan then exit
    python agent.py --status  # print held inventory and exit
    python agent.py --cancel PNR  # mark PNR cancelled in DB (also cancel on delta.com!)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime
from typing import Optional

import config
import db
import notify
import scanner
import booker

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("agent")


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------

def _check_scanner_watchdog() -> None:
    """
    Dead-man's-switch: alert if no prices have been recorded in longer than
    config.SCANNER_WATCHDOG_HOURS. Disabled when watchdog is set to 0.
    Only fires when price history exists (skips first-ever run).
    """
    threshold = config.SCANNER_WATCHDOG_HOURS
    if threshold <= 0:
        return
    hours = db.get_hours_since_last_price()
    if hours is None:
        log.debug("Watchdog: no price history yet — skipping")
        return
    if hours > threshold:
        log.warning(
            "WATCHDOG: no prices recorded in %.1fh (threshold %.1fh) — scanner may be broken",
            hours, threshold,
        )
        notify.scanner_dead(hours_silent=hours, watchdog_threshold=threshold)
    else:
        log.debug("Watchdog: last price %.1fh ago (threshold %.1fh) — OK", hours, threshold)


def run_scan() -> dict:
    """
    One full scan pass across all configured routes × dates.
    Returns summary dict with counts.
    """
    dry_run = config.DRY_RUN
    dates = scanner.build_date_list()
    # Filter to only future dates
    today = date.today().isoformat()
    dates = [d for d in dates if d >= today]

    log.info(
        "%sStarting scan — %d route(s) × %d date(s)",
        "[DRY RUN] " if dry_run else "",
        len(config.ROUTES),
        len(dates),
    )

    _check_scanner_watchdog()
    scan_id = db.start_scan_log(dry_run)
    scan_started_at = datetime.utcnow().isoformat()

    routes_checked  = 0
    dates_checked   = 0
    trigger_count   = 0
    booking_count   = 0
    error_count     = 0

    for route in config.ROUTES:
        routes_checked += 1
        new_bookings_this_route = 0

        for travel_date in dates:
            dates_checked += 1

            # ----------------------------------------------------------
            # 1. Get price
            # ----------------------------------------------------------
            try:
                flight = scanner.get_price(route.origin, route.destination, route.cabin, travel_date)
            except Exception as exc:
                log.error("Scanner error for %s→%s %s: %s", route.origin, route.destination, travel_date, exc)
                error_count += 1
                continue

            if flight is None:
                log.debug("No price returned for %s→%s %s", route.origin, route.destination, travel_date)
                continue

            # ----------------------------------------------------------
            # 2. Record price to history
            # ----------------------------------------------------------
            try:
                db.insert_price(
                    origin=flight.origin,
                    destination=flight.destination,
                    cabin=flight.cabin,
                    travel_date=flight.travel_date,
                    price_usd=flight.price_usd,
                    source=flight.source,
                )
            except Exception as exc:
                log.warning("DB price insert failed: %s", exc)

            log.info(
                "%s→%s %s %s — $%.0f (threshold $%.0f)",
                route.origin, route.destination, route.cabin, travel_date,
                flight.price_usd, route.max_price,
            )

            # ----------------------------------------------------------
            # 3. Check if price is at or below threshold
            # ----------------------------------------------------------
            if flight.price_usd > route.max_price:
                continue

            trigger_count += 1
            log.info(
                "TRIGGER: %s→%s %s $%.0f ≤ $%.0f",
                route.origin, route.destination, travel_date,
                flight.price_usd, route.max_price,
            )
            notify.price_trigger_found(
                origin=route.origin,
                destination=route.destination,
                cabin=route.cabin,
                travel_date=travel_date,
                price=flight.price_usd,
                threshold=route.max_price,
                auto_book=route.auto_book,
            )

            if not route.auto_book:
                log.info("Route is alert-only — skipping booking attempt")
                continue

            # ----------------------------------------------------------
            # 4. Guard: skip if already held
            # ----------------------------------------------------------
            if db.is_already_booked(route.origin, route.destination, route.cabin, travel_date):
                log.info("Already have a held booking for this slot — skipping")
                continue

            # ----------------------------------------------------------
            # 5. Guard: only one new booking per route per scan
            # ----------------------------------------------------------
            if new_bookings_this_route >= config.MAX_NEW_BOOKINGS_PER_ROUTE_PER_SCAN:
                log.info("Per-route booking limit reached — skipping booking for this date")
                continue

            # ----------------------------------------------------------
            # 6. Book it
            # ----------------------------------------------------------
            result = booker.book_flight(
                origin=route.origin,
                destination=route.destination,
                cabin=route.cabin,
                travel_date=travel_date,
                seats=route.seats,
            )

            if result.success and result.pnr:
                booking_count += 1
                new_bookings_this_route += 1
                try:
                    db.insert_booking(
                        pnr=result.pnr,
                        origin=route.origin,
                        destination=route.destination,
                        cabin=route.cabin,
                        travel_date=travel_date,
                        seats=route.seats,
                        price_usd=flight.price_usd,
                        cancel_by=result.cancel_by,
                    )
                except Exception as exc:
                    log.error("Failed to record booking in DB: %s", exc)

                notify.booking_confirmed(
                    pnr=result.pnr,
                    origin=route.origin,
                    destination=route.destination,
                    cabin=route.cabin,
                    travel_date=travel_date,
                    price=flight.price_usd,
                    cancel_by=result.cancel_by,
                    dry_run=result.dry_run,
                )
                log.info(
                    "Booking recorded — PNR: %s  cancel_by: %s",
                    result.pnr, result.cancel_by or "unknown",
                )
            else:
                error_count += 1
                log.error("Booking failed: %s", result.error)
                notify.error_alert(
                    context=f"Booking {route.origin}→{route.destination} {travel_date}",
                    error=result.error or "unknown error",
                )

    # ------------------------------------------------------------------
    # Post-scan: quarantine implausible batches
    #
    # Real premium-cabin fares rarely sit below threshold across the
    # board. If nearly every date "triggered", the scraper was almost
    # certainly recording wrong-cabin prices (see the CI incident where
    # 144/144 dates triggered on economy fares) — purge the batch so it
    # never reaches the charts. Counts stay honest in scan_log so the
    # health monitor can flag the broken sensor.
    # ------------------------------------------------------------------
    if dates_checked >= 10 and trigger_count / dates_checked > config.QUARANTINE_TRIGGER_RATE:
        purged = db.delete_prices_since(scan_started_at)
        log.error(
            "QUARANTINE: %d/%d dates triggered (>%d%%) — purged %d price rows "
            "recorded this scan as implausible (likely wrong-cabin scrape)",
            trigger_count, dates_checked,
            int(config.QUARANTINE_TRIGGER_RATE * 100), purged,
        )

    # ------------------------------------------------------------------
    # Post-scan: check cancel deadlines
    # ------------------------------------------------------------------
    _check_cancel_deadlines()

    # Persist scan log
    db.finish_scan_log(
        scan_id=scan_id,
        routes_checked=routes_checked,
        dates_checked=dates_checked,
        trigger_count=trigger_count,
        booking_count=booking_count,
        error_count=error_count,
    )

    notify.scan_summary(
        routes_checked=routes_checked,
        dates_checked=dates_checked,
        trigger_count=trigger_count,
        booking_count=booking_count,
        dry_run=dry_run,
    )

    summary = dict(
        routes_checked=routes_checked,
        dates_checked=dates_checked,
        trigger_count=trigger_count,
        booking_count=booking_count,
        error_count=error_count,
    )
    log.info(
        "Scan complete — routes=%d  dates=%d  triggers=%d  bookings=%d  errors=%d",
        routes_checked, dates_checked, trigger_count, booking_count, error_count,
    )
    return summary


def _check_cancel_deadlines() -> None:
    """Alert for held bookings approaching their cancellation deadline."""
    for booking in db.get_bookings_approaching_deadline(days=7):
        if not booking["cancel_by"]:
            continue
        cancel_date = date.fromisoformat(booking["cancel_by"])
        days_remaining = (cancel_date - date.today()).days
        if days_remaining < 0:
            continue
        log.warning(
            "Cancel deadline in %d day(s): PNR %s %s→%s %s",
            days_remaining,
            booking["pnr"],
            booking["origin"],
            booking["destination"],
            booking["travel_date"],
        )
        notify.cancel_deadline_warning(
            pnr=booking["pnr"],
            origin=booking["origin"],
            destination=booking["destination"],
            travel_date=booking["travel_date"],
            cancel_by=booking["cancel_by"],
            days_remaining=days_remaining,
        )


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_status() -> None:
    """Print held inventory as a formatted table."""
    rows = db.get_held_bookings()
    if not rows:
        print("No held bookings.")
        return
    col_w = [8, 8, 12, 12, 6, 10, 11, 10]
    headers = ["PNR", "Route", "Cabin", "Date", "Seats", "Price", "Cancel By", "Status"]
    sep = "  ".join("-" * w for w in col_w)
    header = "  ".join(h.ljust(w) for h, w in zip(headers, col_w))
    print(f"\nHeld Inventory — {len(rows)} booking(s)\n")
    print(header)
    print(sep)
    for r in rows:
        route = f"{r['origin']}→{r['destination']}"
        print(
            "  ".join([
                r["pnr"].ljust(col_w[0]),
                route.ljust(col_w[1]),
                r["cabin"].ljust(col_w[2]),
                r["travel_date"].ljust(col_w[3]),
                str(r["seats"]).ljust(col_w[4]),
                f"${r['price_usd']:,.0f}".ljust(col_w[5]),
                (r["cancel_by"] or "—").ljust(col_w[6]),
                r["status"].ljust(col_w[7]),
            ])
        )
    print()


def cmd_cancel(pnr: str) -> None:
    """Mark a PNR cancelled in the local DB."""
    pnr = pnr.strip().upper()
    if db.cancel_booking(pnr):
        print(f"PNR {pnr} marked cancelled in local DB.")
        print(
            "\n*** IMPORTANT: You must ALSO cancel this booking on delta.com or by "
            "calling Delta. The local DB change alone does NOT cancel your ticket. ***\n"
        )
    else:
        print(f"PNR {pnr} not found among held bookings.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="fareTrader — Delta fare monitor and auto-booker")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--once",   action="store_true", help="Run one scan then exit")
    group.add_argument("--status", action="store_true", help="Print held inventory and exit")
    group.add_argument("--cancel", metavar="PNR",       help="Mark PNR cancelled in DB and exit")
    args = parser.parse_args()

    db.init_db()

    if args.status:
        cmd_status()
        return

    if args.cancel:
        cmd_cancel(args.cancel)
        return

    if args.once:
        run_scan()
        return

    # Scheduler loop
    interval_s = config.SCAN_INTERVAL_MINUTES * 60
    log.info(
        "Scheduler started — scan every %d minutes (%s mode)",
        config.SCAN_INTERVAL_MINUTES,
        "DRY RUN" if config.DRY_RUN else "LIVE",
    )
    while True:
        try:
            run_scan()
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as exc:
            log.error("Unhandled scan error: %s", exc, exc_info=True)
            notify.error_alert("Unhandled scan error", str(exc))
        log.info("Sleeping %d minutes until next scan…", config.SCAN_INTERVAL_MINUTES)
        try:
            time.sleep(interval_s)
        except KeyboardInterrupt:
            log.info("Interrupted during sleep — exiting")
            break


if __name__ == "__main__":
    main()
