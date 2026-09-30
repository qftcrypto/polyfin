"""The trader: one decision loop for paper and live.

    python -m polyfin.live.engine            # paper (default)
    python -m polyfin.live.engine --once     # one cycle, then exit
    python -m polyfin.live.engine --live     # live authority (still gated by trade.control)

Each cycle (LOOP_EVERY seconds):
  1. settle filled orders whose market has resolved (recorder writes outcomes)
  2. read the control row and the risk state (both from the database)
  3. price every open market of a liquid series with stage 2
  4. fetch FRESH books for both tokens of those markets (the recorder's are up to 30s old)
  5. for each market without a position: buy the side with the larger edge if
     model_p - ask - fee >= MIN_EDGE, FAK at the highest price that keeps that edge
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime

import psycopg2

from .. import config as mcfg
from ..db import connect
from ..http import get_json
from ..stage1 import load_specs
from ..stage2 import PARAMS_PATH, Stage2
from ..varclock import ET
from . import config as C
from .control import effective
from .executor import LiveExecutor, PaperExecutor
from .fees import fee_per_share
from .sizing import limit_price, shares_for

log = logging.getLogger("trader")

OPEN_STATUSES = ("pending", "filled", "partial", "unknown")


def et_day_start(t: float) -> int:
    d = datetime.fromtimestamp(t, ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


def liquid_series(conn) -> set[str]:
    rows = conn.execute(
        "SELECT series_slug, percentile_cont(0.5) WITHIN GROUP "
        "(ORDER BY COALESCE((raw->>'volume')::float, 0)) FROM markets "
        "WHERE closed = 1 AND end_ts > %s GROUP BY series_slug",
        (int(time.time()) - 8 * 86400,)).fetchall()
    return {s for s, v in rows if v >= C.MIN_SERIES_VOLUME} - C.EXCLUDE_SERIES


def fetch_books(tokens: list[str]) -> dict:
    """token -> {"bids": [(p, size)] best first, "asks": [...], "ts": int}"""
    out = {}
    for i in range(0, len(tokens), 50):
        for b in get_json(f"{mcfg.CLOB}/books", body=[{"token_id": t} for t in tokens[i:i + 50]]):
            bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids") or []),
                          reverse=True)
            asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks") or [])
            out[b["asset_id"]] = {"bids": bids, "asks": asks,
                                  "ts": int(b.get("timestamp") or 0) // 1000}
    return out


def settle(conn) -> int:
    """Close out filled orders whose market has resolved."""
    rows = conn.execute(
        "SELECT o.id, o.side, o.shares_filled, o.avg_price, o.fee, m.outcome_yes "
        "FROM trade.orders o JOIN markets m USING (condition_id) "
        "WHERE o.settled_at IS NULL AND o.shares_filled > 0 AND m.closed = 1 "
        "AND m.outcome_yes IS NOT NULL").fetchall()
    now = int(time.time())
    for oid, side, sh, avg, fee, oy in rows:
        pay = oy if side == "yes" else 1 - oy
        conn.execute("UPDATE trade.orders SET outcome=%s, pnl=%s, settled_at=%s WHERE id=%s",
                     (pay, sh * (pay - avg) - fee, now, oid))
    conn.commit()
    return len(rows)


def risk_state(conn, mode: str, now: float) -> dict:
    day = et_day_start(now)
    r = conn.execute(
        "SELECT "
        " COUNT(*) FILTER (WHERE created_at >= %(day)s AND (shares_filled > 0 OR status IN "
        "   ('pending', 'unknown'))),"
        " COALESCE(SUM(shares_filled * avg_price + fee) FILTER (WHERE settled_at IS NULL "
        "   AND shares_filled > 0), 0)"
        " + COALESCE(SUM(shares_req * limit_price) FILTER (WHERE status IN "
        "   ('pending', 'unknown')), 0),"
        " COALESCE(SUM(pnl) FILTER (WHERE settled_at >= %(day)s), 0),"
        " COUNT(*) FILTER (WHERE shares_filled > 0)"
        " FROM trade.orders WHERE mode = %(mode)s", {"day": day, "mode": mode}).fetchone()
    conn.commit()
    return {"orders_today": r[0], "open_usd": float(r[1]), "pnl_today": float(r[2]),
            "fills_total": r[3]}


def blocked(risk: dict, mode: str, cost: float) -> str | None:
    if risk["orders_today"] >= C.MAX_ORDERS_PER_DAY:
        return "max orders per day"
    if risk["open_usd"] + cost > C.MAX_OPEN_USD:
        return "max open exposure"
    if risk["pnl_today"] <= -C.MAX_DAILY_LOSS_USD:
        return "daily loss limit"
    if mode == "live" and risk["fills_total"] >= C.STOP_AFTER_FILLS_LIVE:
        return "stop-after-fills gate"
    return None


def has_position_or_cooldown(conn, mode: str, cid: str, now: float) -> bool:
    r = conn.execute(
        "SELECT bool_or(status = ANY(%s)), MAX(created_at) FILTER (WHERE status = 'nofill') "
        "FROM trade.orders WHERE mode = %s AND condition_id = %s",
        (list(OPEN_STATUSES), mode, cid)).fetchone()
    conn.commit()
    return bool(r[0]) or (r[1] is not None and now - r[1] < C.NOFILL_COOLDOWN_S)


class Trader:
    def __init__(self, conn, launch_mode: str):
        self.conn = conn
        self.launch_mode = launch_mode
        self.executors = {"paper": PaperExecutor()}
        if launch_mode == "live":
            self.executors["live"] = LiveExecutor()
        self.betas: dict = {}
        self.params = self._params()

    @staticmethod
    def _params() -> dict:
        try:
            return json.loads(PARAMS_PATH.read_text())
        except (OSError, ValueError):
            log.warning("no %s - using stage 2 nowcast without sharpening", PARAMS_PATH)
            return {"gamma": 0.0, "b": 1.0}

    def cycle(self) -> None:
        conn, now = self.conn, time.time()
        n = settle(conn)
        if n:
            log.info("settled %d orders", n)
        mode, reason = effective(conn, self.launch_mode)
        if mode == "paused":
            log.info("paused (%s)", reason)
            return
        newest = conn.execute("SELECT MAX(ts) FROM bars WHERE symbol = ANY(%s)",
                              (mcfg.NOWCAST_FUTURES,)).fetchone()[0]
        conn.commit()
        if newest is None or now - newest > C.MAX_BARS_AGE_S:
            log.warning("bars stale (newest %s) - recorder down? no entries",
                        None if newest is None else f"{now - newest:.0f}s old")
            return

        risk = risk_state(conn, mode, now)
        liquid = liquid_series(conn)
        specs = [s for s in load_specs(conn)
                 if s.outcome is None and s.series_slug in liquid and s.end_ts > now
                 and C.MIN_TAU_S <= s.target_ts - now <= C.MAX_TAU_H * 3600]
        conn.commit()
        model = Stage2(conn, self.params, betas=self.betas)
        priced = []
        for s in specs:
            f = model.features(s, now)
            p = model.prob(s, now)
            if f is None or p is None:
                continue
            if s.target_ts - now < C.NEAR_TIE_TAU_S and abs(f[1]) * 1e4 < C.NEAR_TIE_BP:
                continue
            priced.append((s, p, f))
        tokens = {r[0]: r[1] for r in conn.execute(
            "SELECT condition_id, token_no FROM markets WHERE condition_id = ANY(%s)",
            ([s.condition_id for s, _, _ in priced],))}
        conn.commit()
        books = fetch_books([t for s, _, _ in priced for t in (s.token_yes, tokens[s.condition_id])])

        entered = 0
        for s, p, f in priced:
            best = None
            for side, tok, ps in (("yes", s.token_yes, p), ("no", tokens[s.condition_id], 1 - p)):
                bk = books.get(tok)
                if not bk or not bk["asks"]:
                    continue
                ask, size = bk["asks"][0]
                edge = ps - ask - fee_per_share(ask)
                if C.MIN_PRICE <= ask <= C.MAX_PRICE and (best is None or edge > best[0]):
                    best = (edge, side, tok, ps, bk)
            if best is None or best[0] < C.MIN_EDGE:
                continue
            edge, side, tok, ps, bk = best
            if has_position_or_cooldown(conn, mode, s.condition_id, now):
                continue
            lim = limit_price(ps)
            shares = shares_for(lim) if lim else None
            if not shares:
                log.info("skip %s %s: min size %d x %.2f > $%.0f cap", s.series_slug, side,
                         C.MIN_SHARES, lim or 0, C.MAX_ORDER_USD)
                continue
            why = blocked(risk, mode, shares * lim)
            if why:
                log.info("blocked (%s): %s %s edge %.3f", why, s.series_slug, side, edge)
                continue
            self._enter(mode, s, side, tok, ps, edge, lim, shares, bk, f, now)
            risk = risk_state(conn, mode, time.time())
            entered += 1
        log.info("%s: %d markets priced, %d entries | today %d orders, open $%.2f, pnl $%+.2f",
                 mode, len(priced), entered, risk["orders_today"], risk["open_usd"],
                 risk["pnl_today"])

    def _enter(self, mode, s, side, tok, ps, edge, lim, shares, bk, f, now) -> None:
        conn = self.conn
        feats = {"xr": f[0], "xn": f[1], "v": f[2], "R": f[3], **{k: self.params.get(k)
                 for k in ("gamma", "b", "sharpen_hours")}}
        ask, size = bk["asks"][0]
        try:
            oid = conn.execute(
                "INSERT INTO trade.orders (created_at, mode, condition_id, series_slug, kind, "
                "strike, side, token_id, target_ts, tau_h, model_p, best_bid, best_ask, ask_size,"
                " edge, limit_price, shares_req, asks, features, status) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING id",
                (int(now), mode, s.condition_id, s.series_slug, s.kind, s.strike, side, tok,
                 s.target_ts, (s.target_ts - now) / 3600, ps,
                 bk["bids"][0][0] if bk["bids"] else None, ask, size, edge, lim, shares,
                 json.dumps(bk["asks"][:10]), json.dumps(feats))).fetchone()[0]
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return
        try:
            fill = self.executors[mode].take(tok, lim, shares, bk["asks"])
        except Exception as e:                     # never let one order stop the loop
            log.exception("execution failed")
            conn.execute("UPDATE trade.orders SET status='unknown', error=%s WHERE id=%s",
                         (str(e)[:500], oid))
            conn.commit()
            return
        conn.execute(
            "UPDATE trade.orders SET status=%s, shares_filled=%s, avg_price=%s, fee=%s, "
            "venue_order_id=%s, error=%s, filled_at=%s WHERE id=%s",
            (fill.status, fill.shares, fill.avg_price, fill.fee, fill.venue_order_id,
             fill.error, int(time.time()) if fill.shares else None, oid))
        conn.commit()
        log.info("%s %s %s%s: p=%.3f ask=%.2f edge=%.3f -> %s %.0f @ %s", mode, s.series_slug,
                 side, f" >{s.strike:g}" if s.strike else "", ps, ask, edge, fill.status,
                 fill.shares, f"{fill.avg_price:.3f}" if fill.avg_price else "-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="live authority (row must also say live)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    conn = connect(args.dsn)
    trader = Trader(conn, "live" if args.live else "paper")
    log.info("trader up: launch=%s params=%s", trader.launch_mode, trader.params)
    while True:
        t0 = time.time()
        try:
            trader.cycle()
        except Exception:
            log.exception("cycle failed")
            conn.rollback()
        if args.once:
            return
        time.sleep(max(1.0, C.LOOP_EVERY - (time.time() - t0)))


if __name__ == "__main__":
    main()
