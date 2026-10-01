"""Weekly market discovery and price history (Gamma + CLOB)."""
from __future__ import annotations

import json
import logging
import re
import time

from ..config import CLOB, GAMMA
from ..http import get_json
from ..polymarket import iso_ts
from .config import SERIES

log = logging.getLogger("weekly")


def parse_title(title: str | None):
    """'↑ $4,250' -> ('up', 4250.0); '↓ $735' -> ('down', 735.0)."""
    t = title or ""
    m = re.search(r"\$?\s*(\d[\d,]*(?:\.\d+)?)", t)
    if not m:
        return None
    strike = float(m.group(1).replace(",", ""))
    if "↑" in t:
        return "up", strike
    if "↓" in t:
        return "down", strike
    return None


def parse_market(ev: dict, m: dict, series_slug: str) -> dict | None:
    _, symbol, session = SERIES[series_slug]
    pt = parse_title(m.get("groupItemTitle"))
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
    except ValueError:
        return None
    if not pt or len(tokens) != 2 or not m.get("conditionId"):
        return None
    resolved = bool(m.get("closed")) and m.get("umaResolutionStatus") == "resolved"
    return {
        "condition_id": m["conditionId"], "event_slug": ev["slug"], "series_slug": series_slug,
        "symbol": symbol, "session": session, "direction": pt[0], "strike": pt[1],
        "token_yes": tokens[0], "token_no": tokens[1],
        "created_ts": iso_ts(m.get("createdAt") or ev.get("startDate")),
        "end_ts": iso_ts(m.get("endDate") or ev.get("endDate")),
        "closed": int(resolved),
        "outcome_yes": float(prices[0]) if resolved and prices else None,
        "closed_ts": iso_ts(m.get("closedTime")) if resolved else None,
        "volume": float(m.get("volume") or 0),
        "updated_at": int(time.time()),
        "raw": json.dumps(m, separators=(",", ":")),
    }


UPSERT = """
INSERT INTO weekly.markets (condition_id, event_slug, series_slug, symbol, session, direction,
    strike, token_yes, token_no, created_ts, end_ts, closed, outcome_yes, closed_ts, volume,
    updated_at, raw)
VALUES (%(condition_id)s, %(event_slug)s, %(series_slug)s, %(symbol)s, %(session)s,
    %(direction)s, %(strike)s, %(token_yes)s, %(token_no)s, %(created_ts)s, %(end_ts)s,
    %(closed)s, %(outcome_yes)s, %(closed_ts)s, %(volume)s, %(updated_at)s, %(raw)s)
ON CONFLICT (condition_id) DO UPDATE SET
    closed=excluded.closed, outcome_yes=excluded.outcome_yes, closed_ts=excluded.closed_ts,
    volume=excluded.volume, end_ts=excluded.end_ts, updated_at=excluded.updated_at,
    raw=excluded.raw
"""


def discover(conn, all_weeks: bool = False) -> int:
    """Upsert the markets of each series: the latest few events, or every one."""
    n = 0
    for slug, (sid, _, _) in SERIES.items():
        try:
            evs = get_json(f"{GAMMA}/events?series_id={sid}&order=endDate&ascending=false"
                           f"&limit={100 if all_weeks else 3}")
        except Exception as e:
            log.warning("discovery %s: %s", slug, e)
            continue
        for ev in evs:
            for m in ev.get("markets") or []:
                try:
                    row = parse_market(ev, m, slug)
                except (ValueError, TypeError) as e:
                    log.warning("skip market in %s: %s", ev.get("slug"), e)
                    continue
                if row:
                    conn.execute(UPSERT, row)
                    n += 1
        conn.commit()
    return n


def record_history(conn, fidelity: int = 1) -> int:
    """pm_history for token_yes from the last stored point to close (or now), for every
    market whose history is not yet complete."""
    now = int(time.time())
    rows = conn.execute(
        "SELECT condition_id, token_yes, created_ts, end_ts, closed, closed_ts, history_ts "
        "FROM weekly.markets WHERE NOT (closed = 1 AND history_ts IS NOT NULL "
        "AND history_ts >= LEAST(COALESCE(closed_ts, end_ts), end_ts))").fetchall()
    conn.commit()
    total = 0
    for cid, tok, created, end, closed, closed_ts, hist in rows:
        stop = min(now, min(closed_ts or end, end) + 3600)
        start = max((hist or 0) + 1, (created or stop - 8 * 86400) - 3600)
        if closed and hist and hist >= stop:
            continue
        if start >= stop:
            continue
        try:
            h = get_json(f"{CLOB}/prices-history?market={tok}&startTs={start}&endTs={stop}"
                         f"&fidelity={fidelity}").get("history") or []
        except Exception as e:
            log.warning("history %s: %s", cid[:10], e)
            continue
        conn.executemany("INSERT INTO pm_history VALUES (%s,%s,%s) ON CONFLICT (token_id, ts) "
                         "DO UPDATE SET p=excluded.p",
                         [(tok, int(x["t"]), float(x["p"])) for x in h])
        covered = stop if closed else max((int(x["t"]) for x in h), default=hist)
        if covered:
            conn.execute("UPDATE weekly.markets SET history_ts=%s WHERE condition_id=%s",
                         (covered, cid))
        conn.commit()
        total += len(h)
        time.sleep(0.05)
    return total
