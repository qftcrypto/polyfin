"""Yahoo Finance chart API: 1-minute bars, no key."""
from __future__ import annotations

import logging
import time
import urllib.parse

from .config import LOOKBACK_DAYS, YAHOO
from .http import get_json

log = logging.getLogger("yahoo")


def parse_chart(payload: dict) -> list[tuple]:
    """Chart JSON -> [(ts, o, h, l, c, v)], dropping bars with no close."""
    res = (payload.get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = [q.get(k) or [None] * len(ts) for k in ("open", "high", "low", "close", "volume")]
    # Yahoo stamps the still-forming bar with the request time (e.g. 19:55:37); keep
    # whole minutes only, or that partial bar is stored as an extra, spiky row
    return [(t, *row) for t, *row in zip(ts, *cols) if row[3] is not None and t % 60 == 0]


SPARK_BATCH = 20        # the spark endpoint takes up to 20 symbols per request (45 -> HTTP 400)
RECENT_CLOSES_S = 900   # record_closes writes only the last 15 minutes


def parse_spark(payload: dict) -> dict[str, list[tuple]]:
    """Spark JSON -> {symbol: [(ts, close)]}, whole minutes only, no null closes."""
    out = {}
    for sym, v in (payload or {}).items():
        v = v or {}
        out[sym] = [(t, c) for t, c in zip(v.get("timestamp") or [], v.get("close") or [])
                    if c is not None and t % 60 == 0]
    return out


def fetch_closes(symbols: list[str]) -> dict[str, list[tuple]]:
    q = urllib.parse.quote(",".join(symbols))
    return parse_spark(get_json(f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={q}"
                                f"&range=1d&interval=1m"))


def record_closes(conn, symbols: list[str]) -> int:
    """Today's 1m closes for many symbols per request (spark): fresh prices cheaply.

    Rows it creates have no open/high/low/volume until record_bars fills them; an
    existing full bar only gets its close refreshed.
    """
    total = 0
    for i in range(0, len(symbols), SPARK_BATCH):
        try:
            got = fetch_closes(symbols[i:i + SPARK_BATCH])
        except Exception as e:
            log.warning("spark %s..: %s", symbols[i], e)
            continue
        # only the recent tail: history is record_bars' job, and upserting a whole day
        # for 45 symbols every minute was ~31k rows of needless writes
        cutoff = time.time() - RECENT_CLOSES_S
        rows = [(sym, t, c) for sym, pts in got.items() for t, c in pts if t >= cutoff]
        conn.executemany(
            "INSERT INTO bars (symbol, ts, close) VALUES (%s,%s,%s) "
            "ON CONFLICT (symbol, ts) DO UPDATE SET close = excluded.close", rows)
        conn.commit()
        total += len(rows)
    return total


def fetch_bars(symbol: str, days: int) -> list[tuple]:
    days = max(1, min(days, LOOKBACK_DAYS))
    url = f"{YAHOO}/{urllib.parse.quote(symbol)}?interval=1m&range={days}d&includePrePost=true"
    return parse_chart(get_json(url))


def record_bars(conn, symbols: list[str]) -> int:
    """Upsert recent full bars; the range requested covers the gap since the last FULL bar
    (record_closes keeps the newest row fresh, so MAX(ts) alone would hide an outage)."""
    now = time.time()
    total = 0
    for sym in symbols:
        last = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol=%s AND open IS NOT NULL",
                            (sym,)).fetchone()[0]
        days = LOOKBACK_DAYS if last is None else int((now - last) // 86400) + 1
        try:
            rows = fetch_bars(sym, days)
        except Exception as e:  # one bad symbol must not stall the rest
            log.warning("%s: %s", sym, e)
            continue
        # the newest bar is still forming; upsert so it is overwritten next poll
        conn.executemany(
            "INSERT INTO bars VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(symbol, ts) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            [(sym, *r) for r in rows])
        conn.commit()
        total += len(rows)
        time.sleep(0.3)
    return total
