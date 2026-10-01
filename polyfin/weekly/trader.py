"""Weekly paper trader: buy No on touch strikes the market overprices.

    .venv/bin/python -m polyfin.weekly.trader          # every 2 min (laptop)
    .venv/bin/python -m polyfin.weekly.trader --once

Rule (backtest 2026-10-01, research basis in weekly/backtest.py):
  * model P(touch) = reflection formula on k^2 * remaining session variance
    (k = 0.95, fitted leave-one-week-out on 27 weeks), spot = Yahoo 1m price
    divided by the symbol's proxy offset (gold +0.9% etc., fitted on resolved weeks)
  * buy No when P(no touch) - No ask - fee >= MIN_EDGE, at most ask + 1c
  * $3 per strike, one position per strike, <= $30 per underlying-week
Paper only: nothing here can place an order.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time

from .. import config as mcfg
from ..db import connect
from ..data import load_bars
from ..http import get_json
from ..live.executor import PaperExecutor
from ..live.fees import fee_per_share
from ..live.sizing import exec_limit, shares_for
from .backtest import fit_offsets, load
from .touch import HourlyVol, in_session, prob_touch

log = logging.getLogger("weekly-trader")

ARM = "touch_no"
K = 0.95
MIN_EDGE = 0.10
MIN_ASK, MAX_ASK = 0.03, 0.97
EVENT_CAP_USD = 30.0
NEAR_STRIKE = 0.001          # skip strikes within 0.1% of spot: proxy noise decides those
LOOP_EVERY = 120
NOFILL_COOLDOWN_S = 600
REFIT_EVERY = 6 * 3600       # proxy offsets


def books_for(tokens):
    out = {}
    for i in range(0, len(tokens), 50):
        for b in get_json(f"{mcfg.CLOB}/books", body=[{"token_id": t} for t in tokens[i:i + 50]]):
            out[b["asset_id"]] = {
                "bids": sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []),
                               reverse=True),
                "asks": sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])}
    return out


class WeeklyTrader:
    def __init__(self, conn):
        self.conn = conn
        self.exec = PaperExecutor()
        self.offsets, self.refit_at = {}, 0.0

    def refit(self):
        mk, vols = load(self.conn)
        offs, stats = fit_offsets(mk, vols)
        # all-weeks offset per symbol, for live use
        self.offsets = {sym: st[3] for sym, st in stats.items()}
        self.refit_at = time.time()
        log.info("proxy offsets: %s", {k: f"{100 * v:+.2f}%" for k, v in self.offsets.items()})

    def settle(self):
        rows = self.conn.execute(
            "SELECT o.id, o.side, o.shares_filled, o.avg_price, o.fee, m.outcome_yes "
            "FROM weekly.orders o JOIN weekly.markets m USING (condition_id) "
            "WHERE o.settled_at IS NULL AND o.shares_filled > 0 AND m.closed = 1 "
            "AND m.outcome_yes IS NOT NULL").fetchall()
        for oid, side, sh, avg, fee, oy in rows:
            pay = oy if side == "yes" else 1 - oy
            self.conn.execute("UPDATE weekly.orders SET outcome=%s, pnl=%s, settled_at=%s "
                              "WHERE id=%s", (pay, sh * (pay - avg) - fee, int(time.time()), oid))
        self.conn.commit()
        return len(rows)

    def cycle(self):
        conn, now = self.conn, time.time()
        if now - self.refit_at > REFIT_EVERY:
            self.refit()
        n = self.settle()
        if n:
            log.info("settled %d", n)
        mk = conn.execute(
            "SELECT condition_id, event_slug, symbol, session, direction, strike, token_yes, "
            "token_no, created_ts, end_ts FROM weekly.markets WHERE closed = 0 AND end_ts > %s "
            "AND created_ts < %s", (int(now) + 3600, int(now))).fetchall()
        held = {r[0] for r in conn.execute(
            "SELECT condition_id FROM weekly.orders WHERE arm=%s AND (status IN "
            "('filled','partial') OR (status='nofill' AND created_at > %s))",
            (ARM, int(now) - NOFILL_COOLDOWN_S))}
        spent = dict(conn.execute(
            "SELECT event_slug, SUM(shares_filled * avg_price + fee) FROM weekly.orders "
            "WHERE arm=%s AND shares_filled > 0 GROUP BY event_slug", (ARM,)).fetchall())
        conn.commit()

        state, cands = {}, []
        for cid, ev, sym, session, d, H, ty, tn, created, end in mk:
            if cid in held or sym not in self.offsets:
                continue
            key = (sym, session, created, end)
            if key not in state:
                bars = conn.execute("SELECT ts, open, high, low, close FROM weekly.bars_1h "
                                    "WHERE symbol=%s AND ts > %s ORDER BY ts",
                                    (sym, int(now) - 40 * 86400)).fetchall()
                m1 = conn.execute(
                    "SELECT ts, high, low FROM bars WHERE symbol=%s AND ts >= %s ORDER BY ts",
                    (sym, created)).fetchall()
                conn.commit()
                b = self.offsets[sym]
                sess = [(h, lo) for ts, h, lo in m1 if in_session(ts, session)]
                spot = load_bars(conn, sym).price_at(now)
                conn.commit()
                v = HourlyVol(bars, session).var(now, end, session)
                state[key] = None if (spot is None or v is None) else (
                    spot / (1 + b), K * K * v,
                    max((h for h, _ in sess), default=None), min((lo for _, lo in sess), default=None),
                    b)
            st = state[key]
            if st is None:
                continue
            S, v, hi, lo, b = st
            # already touched (by our data) -> nothing to price
            if d == "up" and hi is not None and hi / (1 + b) >= H:
                continue
            if d == "down" and lo is not None and lo / (1 + b) <= H:
                continue
            if abs(math.log(H / S)) < NEAR_STRIKE:
                continue
            p_no = 1 - prob_touch(d, S, H, v)
            cands.append((cid, ev, sym, d, H, tn, end, S, v, p_no))

        books = books_for([c[5] for c in cands]) if cands else {}
        entered = 0
        for cid, ev, sym, d, H, tn, end, S, v, p_no in cands:
            bk = books.get(tn)
            if not bk or not bk["asks"]:
                continue
            ask, size = bk["asks"][0]
            if not MIN_ASK <= ask <= MAX_ASK:
                continue
            edge = p_no - ask - fee_per_share(ask)
            if edge < MIN_EDGE:
                continue
            lim = exec_limit(p_no, MIN_EDGE, ask)
            shares = shares_for(lim) if lim else None
            if not shares or spent.get(ev, 0.0) + shares * lim > EVENT_CAP_USD:
                continue
            fill = self.exec.take(tn, lim, shares, bk["asks"])
            conn.execute(
                "INSERT INTO weekly.orders (created_at, arm, condition_id, event_slug, symbol, "
                "direction, strike, side, token_id, end_ts, tau_h, spot, var_left, model_p, "
                "best_bid, best_ask, ask_size, edge, limit_price, shares_req, asks, status, "
                "shares_filled, avg_price, fee) VALUES (%s,%s,%s,%s,%s,%s,%s,'no',%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(now), ARM, cid, ev, sym, d, H, tn, end, (end - now) / 3600, S, v, p_no,
                 bk["bids"][0][0] if bk["bids"] else None, ask, size, edge, lim, shares,
                 json.dumps(bk["asks"][:10]), fill.status, fill.shares, fill.avg_price, fill.fee))
            conn.commit()
            if fill.shares:
                spent[ev] = spent.get(ev, 0.0) + fill.shares * fill.avg_price + fill.fee
                entered += 1
                log.info("paper %s No %s %s $%g: p_no=%.3f ask=%.2f edge=%.3f -> %s %.0f @ %.3f",
                         ARM, sym, d, H, p_no, ask, edge, fill.status, fill.shares, fill.avg_price)
        log.info("%d open strikes priced, %d entries", len(cands), entered)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    conn = connect()
    tr = WeeklyTrader(conn)
    while True:
        t0 = time.time()
        try:
            tr.cycle()
        except Exception:
            log.exception("cycle failed")
            conn.rollback()
        if args.once:
            return
        time.sleep(max(5.0, LOOP_EVERY - (time.time() - t0)))


if __name__ == "__main__":
    main()
