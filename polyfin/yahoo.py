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
    return [(t, *row) for t, *row in zip(ts, *cols) if row[3] is not None]


def fetch_bars(symbol: str, days: int) -> list[tuple]:
    days = max(1, min(days, LOOKBACK_DAYS))
    url = f"{YAHOO}/{urllib.parse.quote(symbol)}?interval=1m&range={days}d&includePrePost=true"
    return parse_chart(get_json(url))


def record_bars(conn, symbols: list[str]) -> int:
    """Upsert recent bars; the range requested covers the gap since the last stored bar."""
    now = time.time()
    total = 0
    for sym in symbols:
        last = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol=?", (sym,)).fetchone()[0]
        days = LOOKBACK_DAYS if last is None else int((now - last) // 86400) + 1
        try:
            rows = fetch_bars(sym, days)
        except Exception as e:  # one bad symbol must not stall the rest
            log.warning("%s: %s", sym, e)
            continue
        # the newest bar is still forming; upsert so it is overwritten next poll
        conn.executemany(
            "INSERT INTO bars VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol, ts) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            [(sym, *r) for r in rows])
        conn.commit()
        total += len(rows)
        time.sleep(0.3)
    return total
