"""Polymarket side: market discovery (Gamma), price history and books (CLOB)."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime

from .config import BOOK_DEPTH, CLOB, GAMMA, LOOKBACK_DAYS, SERIES
from .http import get_json

log = logging.getLogger("polymarket")

BOOKS_BATCH = 50


def iso_ts(s: str | None) -> int | None:
    if not s:
        return None
    # py3.9 fromisoformat rejects 2-digit fractions like '12:00:59.02'; drop them
    s = re.sub(r"\.\d+", "", s.strip().replace("Z", "+00:00").replace(" ", "T", 1))
    if re.search(r"[+-]\d\d$", s):
        s += ":00"
    return int(datetime.fromisoformat(s).timestamp())


def parse_strike(title: str | None) -> float | None:
    """'$795' / '↑ $1,234.5' -> 795.0 / 1234.5"""
    m = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)", title or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_market(ev: dict, m: dict, series_slug: str) -> dict | None:
    _, kind, symbol = SERIES[series_slug]
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
    except ValueError:
        return None
    if len(tokens) != 2 or not m.get("conditionId"):
        return None
    resolved = bool(m.get("closed")) and m.get("umaResolutionStatus") == "resolved"
    return {
        "condition_id": m["conditionId"],
        "event_slug": ev["slug"],
        "series_slug": series_slug,
        "kind": kind,
        "symbol": symbol,
        "question": m.get("question"),
        "strike": parse_strike(m.get("groupItemTitle")) if kind == "strikes" else None,
        "token_yes": tokens[0],
        "token_no": tokens[1],
        "event_start": iso_ts(m.get("eventStartTime") or ev.get("startTime")),
        "end_ts": iso_ts(m.get("endDate") or ev.get("endDate")),
        "closed": int(resolved),
        "outcome_yes": float(prices[0]) if resolved and prices else None,
        "resolution_source": m.get("resolutionSource"),
        "updated_at": int(time.time()),
        "raw": json.dumps(m, separators=(",", ":")),
    }


UPSERT_MARKET = """
INSERT INTO markets (condition_id, event_slug, series_slug, kind, symbol, question, strike,
    token_yes, token_no, event_start, end_ts, closed, outcome_yes, resolution_source,
    updated_at, raw)
VALUES (%(condition_id)s, %(event_slug)s, %(series_slug)s, %(kind)s, %(symbol)s, %(question)s, %(strike)s,
    %(token_yes)s, %(token_no)s, %(event_start)s, %(end_ts)s, %(closed)s, %(outcome_yes)s, %(resolution_source)s,
    %(updated_at)s, %(raw)s)
ON CONFLICT(condition_id) DO UPDATE SET
    question=excluded.question, event_start=excluded.event_start, end_ts=excluded.end_ts,
    closed=excluded.closed, outcome_yes=excluded.outcome_yes,
    resolution_source=excluded.resolution_source, updated_at=excluded.updated_at,
    raw=excluded.raw
"""


def discover(conn) -> int:
    """Upsert every market in each series that settles within the lookback or later."""
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    n = 0
    for slug, (sid, _, _) in SERIES.items():
        try:
            evs = get_json(f"{GAMMA}/events?series_id={sid}&order=endDate"
                           f"&ascending=false&limit={LOOKBACK_DAYS + 5}")
        except Exception as e:
            log.warning("discovery %s: %s", slug, e)
            continue
        for ev in evs:
            if (iso_ts(ev.get("endDate")) or 0) < cutoff:
                continue
            for m in ev.get("markets") or []:
                try:
                    row = parse_market(ev, m, slug)
                except (ValueError, TypeError) as e:
                    log.warning("skip market in %s: %s", ev.get("slug"), e)
                    continue
                if row:
                    conn.execute(UPSERT_MARKET, row)
                    n += 1
        conn.commit()
    return n


def record_history(conn) -> int:
    """Fill prices-history for token_yes from the last stored point up to settlement."""
    now = int(time.time())
    floor = now - LOOKBACK_DAYS * 86400
    rows = conn.execute(
        "SELECT condition_id, token_yes, end_ts, closed, history_ts FROM markets "
        "WHERE end_ts >= %s", (floor,)).fetchall()
    total = 0
    for cid, tok, end_ts, closed, hist_ts in rows:
        stop = min(now, (end_ts or now) + 3600)
        if closed and hist_ts and hist_ts >= (end_ts or 0):
            continue  # resolved and fully recorded
        start = max(floor, (hist_ts or 0) + 1)
        if start >= stop:
            continue
        try:
            h = get_json(f"{CLOB}/prices-history?market={tok}&startTs={start}"
                         f"&endTs={stop}&fidelity=1").get("history") or []
        except Exception as e:
            log.warning("history %s: %s", cid[:10], e)
            continue
        conn.executemany("INSERT INTO pm_history VALUES (%s,%s,%s) ON CONFLICT (token_id, ts) "
                         "DO UPDATE SET p=excluded.p",
                         [(tok, int(x["t"]), float(x["p"])) for x in h])
        # history_ts = covered-through; a resolved market is done once fetched to `stop`,
        # even if its last point is earlier (no more points will ever appear)
        covered = stop if closed else max((int(x["t"]) for x in h), default=hist_ts)
        if covered:
            conn.execute("UPDATE markets SET history_ts=%s WHERE condition_id=%s", (covered, cid))
        conn.commit()
        total += len(h)
        time.sleep(0.1)
    return total


def parse_book(b: dict) -> tuple:
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []),
                  reverse=True)[:BOOK_DEPTH]
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])[:BOOK_DEPTH]
    ts = int(b.get("timestamp") or time.time() * 1000) // 1000
    return (b["asset_id"], ts,
            bids[0][0] if bids else None, bids[0][1] if bids else None,
            asks[0][0] if asks else None, asks[0][1] if asks else None,
            json.dumps(bids), json.dumps(asks))


def record_books(conn) -> int:
    """Snapshot the Yes book of every market that has not settled yet."""
    toks = [r[0] for r in conn.execute(
        "SELECT token_yes FROM markets WHERE closed=0 AND end_ts > %s", (int(time.time()),))]
    total = 0
    for i in range(0, len(toks), BOOKS_BATCH):
        chunk = toks[i:i + BOOKS_BATCH]
        try:
            books = get_json(f"{CLOB}/books", body=[{"token_id": t} for t in chunk])
        except Exception as e:
            log.warning("books: %s", e)
            continue
        conn.executemany("INSERT INTO pm_books VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                         "ON CONFLICT DO NOTHING",
                         [parse_book(b) for b in books if b.get("asset_id")])
        conn.commit()
        total += len(books)
    return total
