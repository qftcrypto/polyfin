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
from ..settings import Credentials
from .control import effective, set_mode
from .executor import PaperExecutor
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
        # a live fill settles only once the venue has confirmed it (reconcile.py)
        "AND (o.mode = 'paper' OR o.reconciled_at IS NOT NULL) "
        "AND m.outcome_yes IS NOT NULL").fetchall()
    now = int(time.time())
    for oid, side, sh, avg, fee, oy in rows:
        pay = oy if side == "yes" else 1 - oy
        conn.execute("UPDATE trade.orders SET outcome=%s, pnl=%s, settled_at=%s WHERE id=%s",
                     (pay, sh * (pay - avg) - fee, now, oid))
    conn.commit()
    return len(rows)


def slot_for(tau_s: float) -> str:
    return "late" if tau_s <= C.LATE_WINDOW_H * 3600 else "early"


def risk_state(conn, mode: str, now: float) -> dict:
    """Totals, plus orders_today / open_usd per slot under "slots"."""
    day = et_day_start(now)
    rows = conn.execute(
        "SELECT slot,"
        " COUNT(*) FILTER (WHERE created_at >= %(day)s AND (shares_filled > 0 OR status IN "
        "   ('pending', 'unknown'))),"
        " COALESCE(SUM(shares_filled * avg_price + fee) FILTER (WHERE settled_at IS NULL "
        "   AND shares_filled > 0), 0)"
        " + COALESCE(SUM(shares_req * limit_price) FILTER (WHERE status IN "
        "   ('pending', 'unknown')), 0),"
        " COALESCE(SUM(pnl) FILTER (WHERE settled_at >= %(day)s), 0),"
        " COUNT(*) FILTER (WHERE shares_filled > 0)"
        " FROM trade.orders WHERE mode = %(mode)s GROUP BY slot",
        {"day": day, "mode": mode}).fetchall()
    conn.commit()
    slots = {s: {"orders_today": 0, "open_usd": 0.0} for s in C.MAX_OPEN_USD_SLOT}
    risk = {"orders_today": 0, "open_usd": 0.0, "pnl_today": 0.0, "fills_total": 0,
            "slots": slots}
    for slot, n, open_usd, pnl, fills in rows:
        if slot in slots:
            slots[slot] = {"orders_today": n, "open_usd": float(open_usd)}
        risk["orders_today"] += n
        risk["open_usd"] += float(open_usd)
        risk["pnl_today"] += float(pnl)
        risk["fills_total"] += fills
    return risk


def blocked(risk: dict, mode: str, cost: float, slot: str) -> str | None:
    sr = risk["slots"][slot]
    if sr["orders_today"] >= C.MAX_ORDERS_PER_DAY_SLOT[slot]:
        return f"max {slot} orders per day"
    if sr["open_usd"] + cost > C.MAX_OPEN_USD_SLOT[slot]:
        return f"max {slot} open exposure"
    if risk["open_usd"] + cost > C.MAX_OPEN_USD:
        return "max open exposure"
    if risk["pnl_today"] <= -C.MAX_DAILY_LOSS_USD:
        return "daily loss limit"
    if mode == "live" and risk["fills_total"] >= C.STOP_AFTER_FILLS_LIVE:
        return "stop-after-fills gate"
    return None


def has_position_or_cooldown(conn, mode: str, cid: str, slot: str, now: float) -> bool:
    r = conn.execute(
        "SELECT bool_or(status = ANY(%s)), MAX(created_at) FILTER (WHERE status = 'nofill') "
        "FROM trade.orders WHERE mode = %s AND condition_id = %s AND slot = %s",
        (list(OPEN_STATUSES), mode, cid, slot)).fetchone()
    conn.commit()
    return bool(r[0]) or (r[1] is not None and now - r[1] < C.NOFILL_COOLDOWN_S)


class Trader:
    def __init__(self, conn, launch_mode: str):
        self.conn = conn
        self.launch_mode = launch_mode
        self.executors = {"paper": PaperExecutor()}
        self.clob = self.chain = self.relayer = None
        self.last_redeem = 0.0
        if launch_mode == "live":
            from .clob import ClobExecutor
            self.creds = Credentials()
            self.clob = ClobExecutor(self.creds)          # raises if FIN_ creds are missing
            self.executors["live"] = self.clob
            self._init_chain()
        self.betas: dict = {}
        self.params_mtime = None
        self.params = self._params()

    def _init_chain(self) -> None:
        """Redemption fallback only; trading works without it (auto-redeem)."""
        from .chain import Chain, Relayer
        try:
            self.chain = Chain(self.creds.rpc_url, self.creds.funder)
        except Exception as e:
            log.warning("no Polygon RPC - redemption fallback disabled: %s", str(e)[:120])
        try:
            self.relayer = Relayer(self.creds.relayer_api_key, self.creds.relayer_api_key_address)
        except ValueError as e:
            log.warning("%s - redemption fallback disabled", e)

    def _params(self) -> dict:
        try:
            self.params_mtime = PARAMS_PATH.stat().st_mtime
            return json.loads(PARAMS_PATH.read_text())
        except (OSError, ValueError):
            log.warning("no %s - using stage 2 nowcast without sharpening", PARAMS_PATH)
            return {"gamma": 0.0, "b": 1.0}

    def _reload_params(self) -> None:
        """Pick up a refit (deploy/polyfin-refit.timer) without a restart."""
        try:
            m = PARAMS_PATH.stat().st_mtime
        except OSError:
            return
        if m != self.params_mtime:
            self.params = self._params()
            self.betas.clear()
            log.info("stage 2 params reloaded: %s", self.params)

    def cycle(self) -> None:
        conn, now = self.conn, time.time()
        if self.clob is not None:                      # live bookkeeping runs even paused
            from .reconcile import reconcile, redeem
            reconcile(conn, self.clob)
            if now - self.last_redeem > 300:
                redeem(conn, self.chain, self.relayer, self.creds)
                self.last_redeem = now
        # a paper row left 'pending' by a crash would block its market forever
        conn.execute("UPDATE trade.orders SET status='nofill', error='stale pending' "
                     "WHERE mode='paper' AND status='pending' AND created_at < %s", (now - 60,))
        conn.commit()
        n = settle(conn)
        if n:
            log.info("settled %d orders", n)
        mode, reason = effective(conn, self.launch_mode)
        if mode == "paused":
            log.info("paused (%s)", reason)
            return
        # recorder liveness: the newest bar of ANY symbol.  Not the futures -
        # Yahoo serves CME bars 10-20 minutes late, so they always look stale.
        newest = conn.execute("SELECT MAX(ts) FROM bars WHERE ts > %s",
                              (int(now) - 6 * 3600,)).fetchone()[0]
        conn.commit()
        if newest is None or now - newest > C.MAX_BARS_AGE_S:
            log.warning("bars stale (newest %s) - recorder down? no entries",
                        None if newest is None else f"{now - newest:.0f}s old")
            return

        self._reload_params()
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

        balance = self.clob.collateral_balance() if mode == "live" else None
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
            slot = slot_for(s.target_ts - now)
            if has_position_or_cooldown(conn, mode, s.condition_id, slot, now):
                continue
            lim = limit_price(ps)
            shares = shares_for(lim) if lim else None
            if not shares:
                log.info("skip %s %s: min size %d x %.2f > $%.0f cap", s.series_slug, side,
                         C.MIN_SHARES, lim or 0, C.MAX_ORDER_USD)
                continue
            why = blocked(risk, mode, shares * lim, slot)
            if why == "stop-after-fills gate":
                # latched in the control row, so a restart cannot resume it
                set_mode(conn, "paused", f"live stop-after-fills gate "
                         f"({C.STOP_AFTER_FILLS_LIVE} fills) - review, then `control live`")
                log.warning("stop-after-fills gate reached: trading paused for review")
                break
            if why is None and mode == "live":
                cost = shares * lim * 1.02                 # + fee headroom
                if balance is None:
                    why = "balance unknown"
                elif balance < cost:
                    why = f"balance ${balance:.2f} < ${cost:.2f}"
            if why:
                log.info("blocked (%s): %s %s edge %.3f", why, s.series_slug, side, edge)
                continue
            self._enter(mode, s, side, tok, ps, edge, lim, shares, bk, f, now, slot)
            if balance is not None:
                balance -= shares * lim * 1.02
            risk = risk_state(conn, mode, time.time())
            entered += 1
        sl = risk["slots"]
        log.info("%s: %d markets priced, %d entries | today %d orders (early %d, late %d), "
                 "open $%.2f (early $%.2f, late $%.2f), pnl $%+.2f", mode, len(priced), entered,
                 risk["orders_today"], sl["early"]["orders_today"], sl["late"]["orders_today"],
                 risk["open_usd"], sl["early"]["open_usd"], sl["late"]["open_usd"],
                 risk["pnl_today"])

    def _enter(self, mode, s, side, tok, ps, edge, lim, shares, bk, f, now, slot) -> None:
        conn = self.conn
        feats = {"xr": f[0], "xn": f[1], "v": f[2], "R": f[3], **{k: self.params.get(k)
                 for k in ("gamma", "b", "sharpen_hours")}}
        ask, size = bk["asks"][0]
        try:
            oid = conn.execute(
                "INSERT INTO trade.orders (created_at, mode, condition_id, series_slug, kind, "
                "strike, side, token_id, target_ts, tau_h, model_p, best_bid, best_ask, ask_size,"
                " edge, limit_price, shares_req, asks, features, slot, status) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') "
                "RETURNING id",
                (int(now), mode, s.condition_id, s.series_slug, s.kind, s.strike, side, tok,
                 s.target_ts, (s.target_ts - now) / 3600, ps,
                 bk["bids"][0][0] if bk["bids"] else None, ask, size, edge, lim, shares,
                 json.dumps(bk["asks"][:10]), json.dumps(feats), slot)).fetchone()[0]
            conn.commit()
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return
        try:
            label = f"{s.series_slug} {side}" + (f" >{s.strike:g}" if s.strike else "")
            fill = self.executors[mode].take(tok, lim, shares, bk["asks"], label=label)
        except Exception as e:                     # never let one order stop the loop
            log.exception("execution failed")
            conn.execute("UPDATE trade.orders SET status='unknown', error=%s WHERE id=%s",
                         (str(e)[:500], oid))
            conn.commit()
            return
        conn.execute(
            "UPDATE trade.orders SET status=%s, shares_filled=%s, avg_price=%s, fee=%s, "
            "venue_order_id=%s, error=%s, filled_at=%s, trade_ids=%s, settle_state=%s "
            "WHERE id=%s",
            (fill.status, fill.shares, fill.avg_price, fill.fee, fill.venue_order_id,
             fill.error, int(time.time()) if fill.shares else None,
             json.dumps(list(fill.trade_ids)) if fill.trade_ids else None,
             "confirmed" if fill.settled else None, oid))
        conn.commit()
        log.info("%s [%s] %s %s%s: p=%.3f ask=%.2f edge=%.3f -> %s %.0f @ %s", mode, slot,
                 s.series_slug, side, f" >{s.strike:g}" if s.strike else "", ps, ask, edge, fill.status,
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
