"""Hyperliquid perp 1m candles as real-time model inputs (recorded, not traded).

Yahoo serves CME futures ~10-11 min late (measured live 2026-10-14: CL=F's newest
bar 10.6-10.9 min old).  Hyperliquid's xyz:CL - a WTI perpetual on the trade.xyz
HIP-3 dex, ~$128M/day, 0.1c spread - trades 24/7 with a public, keyless API.
Its 1-minute returns match CL=F's at the same timestamps (corr 0.98 over 3 days);
the level differs (basis ~ -$0.78), so only its RETURNS are meaningful.

Bars go into `bars` as symbol "HL:<coin>" (e.g. HL:CL).  They are excluded from
the trader's recorder-liveness check: a 24/7 perp would hide a weekend or a dead
Yahoo feed.
"""
from __future__ import annotations

import json
import time
import urllib.request

INFO = "https://api.hyperliquid.xyz/info"
PERPS = {"xyz:CL": "HL:CL"}          # coin -> bars symbol
BACKFILL_S = 3 * 86400               # first run (candleSnapshot returns <= 5000 candles)
OVERLAP_S = 10 * 60                  # re-fetch the last minutes: the newest candle is still forming


def _post(body: dict):
    req = urllib.request.Request(INFO, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def parse_candles(rows: list[dict]) -> list[tuple]:
    """Hyperliquid candles -> (ts, open, high, low, close, volume), ts = minute start (s)."""
    out = []
    for k in rows:
        ts = int(k["t"]) // 1000
        if ts % 60 == 0:
            out.append((ts, float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])))
    return out


def record_perps(conn) -> int:
    now = int(time.time())
    total = 0
    for coin, sym in PERPS.items():
        last = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol = %s", (sym,)).fetchone()[0]
        conn.commit()
        start = now - BACKFILL_S if last is None else int(last) - OVERLAP_S
        rows = parse_candles(_post({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "1m", "startTime": start * 1000, "endTime": now * 1000}}))
        conn.executemany(
            "INSERT INTO bars VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (symbol, ts) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "volume=excluded.volume", [(sym, *r) for r in rows])
        conn.commit()
        total += len(rows)
    return total
