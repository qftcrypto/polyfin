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
import math
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
from .sizing import exec_limit, shares_for

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


def blend_with_market(p: float, yes_book, w) -> float | None:
    """logit P = w[0] logit(p) + w[1] logit(Yes mid); None without a two-sided book."""
    if not yes_book or not yes_book["bids"] or not yes_book["asks"]:
        return None
    mid = (yes_book["bids"][0][0] + yes_book["asks"][0][0]) / 2
    lg = lambda q: math.log(min(max(q, 1e-3), 1 - 1e-3) / (1 - min(max(q, 1e-3), 1 - 1e-3)))
    return 1 / (1 + math.exp(-(w[0] * lg(p) + w[1] * lg(mid))))


def slot_for(tau_s: float) -> str:
    return "late" if tau_s <= C.LATE_WINDOW_H * 3600 else "early"


def risk_state(conn, mode: str, now: float, arm: str = "base") -> dict:
    """Totals plus orders_today / open_usd per slot under "slots": per arm for paper
    (each arm is its own experiment), across ALL arms for live - the caps protect one
    wallet, whatever the arm is called."""
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
        " FROM trade.orders WHERE mode = %(mode)s AND (%(mode)s = 'live' OR arm = %(arm)s) "
        "GROUP BY slot",
        {"day": day, "mode": mode, "arm": arm}).fetchall()
    conn.commit()
    slots = {s: {"orders_today": 0, "open_usd": 0.0} for s in ("early", "late")}
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
    lim = C.LIMITS[mode]
    if lim is None:                                     # paper: no caps
        return None
    sr = risk["slots"][slot]
    if "orders_per_day_slot" in lim and sr["orders_today"] >= lim["orders_per_day_slot"][slot]:
        return f"max {slot} orders per day"
    if "open_usd_slot" in lim and sr["open_usd"] + cost > lim["open_usd_slot"][slot]:
        return f"max {slot} open exposure"
    if "open_usd" in lim and risk["open_usd"] + cost > lim["open_usd"]:
        return "max open exposure"
    if "daily_loss" in lim and risk["pnl_today"] <= -lim["daily_loss"]:
        return "daily loss limit"
    if (mode == "live" and C.STOP_AFTER_FILLS_LIVE is not None
            and risk["fills_total"] >= C.STOP_AFTER_FILLS_LIVE):
        return "stop-after-fills gate"
    return None


def position_state(conn, mode: str, arm: str, cid: str, slot: str):
    """(held legs as [(leg, side)] in leg order, last no-fill time) for one market slot."""
    rows = conn.execute(
        "SELECT leg, side, status, created_at FROM trade.orders "
        "WHERE mode = %s AND arm = %s AND condition_id = %s AND slot = %s",
        (mode, arm, cid, slot)).fetchall()
    conn.commit()
    held = sorted((leg, side) for leg, side, st, _ in rows if st in OPEN_STATUSES)
    last_nofill = max((t for _, _, st, t in rows if st == "nofill"), default=None)
    return held, last_nofill


def repeat_state(conn, mode: str, arm: str, cid: str, slot: str) -> dict:
    """Buys held in one market slot of a repeat arm, and what limits the next one."""
    rows = conn.execute(
        "SELECT leg, side, status, created_at, shares_filled, avg_price, fee, shares_req, "
        "limit_price FROM trade.orders WHERE mode = %s AND arm = %s AND condition_id = %s "
        "AND slot = %s ORDER BY created_at",
        (mode, arm, cid, slot)).fetchall()
    conn.commit()
    held = [r for r in rows if r[2] in OPEN_STATUSES]
    cost = sum((r[4] * r[5] + r[6]) if r[4] > 0 else (r[7] * r[8] if r[2] in ("pending", "unknown")
                                                     else 0.0) for r in held)
    return {"n": len(held), "last_side": held[-1][1] if held else None,
            "last_buy": held[-1][3] if held else None, "cost": cost,
            "next_leg": max((r[0] or 0 for r in rows), default=0) + 1,
            "last_nofill": max((r[3] for r in rows if r[2] == "nofill"), default=None)}


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
        self.seen: dict = {}            # confirm arms: (arm, cid, side, leg) -> first seen ts
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
            day_sd = model.m1._get(s.symbol)[1].daily_vol()
            if s.kind != "strikes" and day_sd > 0 and abs(f[1]) > C.MAX_MOVE_DAILY_SD * day_sd:
                log.warning("skip %s: implied move %+.2f%% is > %.0f daily sd (%.2f%%) - "
                            "bad price data?", s.series_slug, 100 * f[1], C.MAX_MOVE_DAILY_SD,
                            100 * day_sd)
                continue
            priced.append((s, p, f))
        tokens = {r[0]: r[1] for r in conn.execute(
            "SELECT condition_id, token_no FROM markets WHERE condition_id = ANY(%s)",
            ([s.condition_id for s, _, _ in priced],))}
        conn.commit()
        books = fetch_books([t for s, _, _ in priced for t in (s.token_yes, tokens[s.condition_id])])

        balance = self.clob.collateral_balance() if mode == "live" else None
        for arm, cfg in C.ARMS[mode].items():
            run = self._run_repeat_arm if "repeat" in cfg else self._run_arm
            balance = run(mode, arm, cfg, priced, tokens, books, now, balance)

    def _run_arm(self, mode, arm, cfg, priced, tokens, books, now, balance):
        """Enter what this arm's rule allows; returns the balance left (live only)."""
        conn = self.conn
        risk = risk_state(conn, mode, now, arm)
        entered = 0
        rungs = cfg.get("rungs", [cfg["min_edge"]])
        confirmed_now: set = set()
        for s, p, f in priced:
            slot = slot_for(s.target_ts - now)
            if slot not in cfg["slots"]:
                continue
            if "max_tau_h" in cfg and s.target_ts - now > cfg["max_tau_h"] * 3600:
                continue
            if "blend" in cfg:
                p = blend_with_market(p, books.get(s.token_yes), cfg["blend"])
                if p is None:
                    continue
            sides = [("yes", s.token_yes, p), ("no", tokens[s.condition_id], 1 - p)]
            best = None
            for side, tok, ps in sides:
                bk = books.get(tok)
                if not bk or not bk["asks"]:
                    continue
                ask, size = bk["asks"][0]
                edge = ps - ask - fee_per_share(ask)
                if C.MIN_PRICE <= ask <= C.MAX_PRICE and (best is None or edge > best[0]):
                    best = (edge, side, tok, ps, bk)
            if best is None or best[0] < rungs[0]:
                continue                                # nothing reaches even the first rung
            held, last_nofill = position_state(conn, mode, arm, s.condition_id, slot)
            if len(held) >= len(rungs):
                continue
            if last_nofill is not None and now - last_nofill < C.NOFILL_COOLDOWN_S:
                continue
            leg, need = len(held) + 1, rungs[len(held)]
            if held:                                    # add-on legs stay on leg 1's side
                side0 = held[0][1]
                best = None
                for side, tok, ps in sides:
                    bk = books.get(tok)
                    if side == side0 and bk and bk["asks"]:
                        ask = bk["asks"][0][0]
                        if C.MIN_PRICE <= ask <= C.MAX_PRICE:
                            best = (ps - ask - fee_per_share(ask), side, tok, ps, bk)
            if best is None or best[0] < need:
                continue
            edge, side, tok, ps, bk = best
            if cfg.get("confirm_s"):
                key = (arm, s.condition_id, side, leg)
                confirmed_now.add(key)
                first = self.seen.setdefault(key, now)
                if now - first < cfg["confirm_s"]:
                    continue                            # wait for the signal to hold
            if mode == "live":
                fresh = self._refresh(tok, ps, need, s, side, arm)
                if fresh is None:
                    continue
                bk, edge = fresh
            lim = exec_limit(ps, need, bk["asks"][0][0])
            shares = shares_for(lim) if lim else None
            if not shares:
                log.info("[%s] skip %s %s: min size %d x %.2f > $%.0f cap", arm, s.series_slug,
                         side, C.MIN_SHARES, lim or 0, C.MAX_ORDER_USD)
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
                log.info("[%s] blocked (%s): %s %s edge %.3f", arm, why, s.series_slug, side,
                         edge)
                continue
            self._enter(mode, arm, s, side, tok, ps, edge, lim, shares, bk, f, now, slot, leg)
            if balance is not None:
                balance -= shares * lim * 1.02
            risk = risk_state(conn, mode, time.time(), arm)
            entered += 1
        if cfg.get("confirm_s"):                        # a signal that lapsed starts over
            for k in [k for k in self.seen if k[0] == arm and k not in confirmed_now]:
                del self.seen[k]
        sl = risk["slots"]
        log.info("%s/%s: %d markets priced, %d entries | today %d orders (early %d, late %d), "
                 "open $%.2f (early $%.2f, late $%.2f), pnl $%+.2f", mode, arm, len(priced),
                 entered, risk["orders_today"], sl["early"]["orders_today"],
                 sl["late"]["orders_today"], risk["open_usd"], sl["early"]["open_usd"],
                 sl["late"]["open_usd"], risk["pnl_today"])
        return balance

    def _run_repeat_arm(self, mode, arm, cfg, priced, tokens, books, now, balance):
        """Buy again whenever an edge persists (research/repeat.py).

        Each buy needs edge >= min_edge on the side of the latest buy, or >= cfg["flip"]
        on the other side (never, if flip is None).  Buys are >= spacing apart and a
        market holds at most max_buys buys and market_cap_usd of cost.
        """
        conn, rp = self.conn, cfg["repeat"]
        risk = risk_state(conn, mode, now, arm)
        entered = 0
        for s, p, f in priced:
            slot = slot_for(s.target_ts - now)
            if slot not in cfg["slots"]:
                continue
            st = repeat_state(conn, mode, arm, s.condition_id, slot)
            if st["n"] >= rp["max_buys"]:
                continue
            if st["last_buy"] is not None and now - st["last_buy"] < rp["spacing_s"]:
                continue
            if st["last_nofill"] is not None and now - st["last_nofill"] < C.NOFILL_COOLDOWN_S:
                continue
            best = None
            for side, tok, ps in (("yes", s.token_yes, p), ("no", tokens[s.condition_id], 1 - p)):
                bk = books.get(tok)
                if not bk or not bk["asks"]:
                    continue
                ask = bk["asks"][0][0]
                if not C.MIN_PRICE <= ask <= C.MAX_PRICE:
                    continue
                if st["last_side"] is None or side == st["last_side"]:
                    need = cfg["min_edge"]
                elif cfg["flip"] is None:
                    continue
                else:
                    need = cfg["flip"]
                edge = ps - ask - fee_per_share(ask)
                if edge >= need and (best is None or edge > best[0]):
                    best = (edge, side, tok, ps, bk, need)
            if best is None:
                continue
            edge, side, tok, ps, bk, need = best
            if mode == "live":
                fresh = self._refresh(tok, ps, need, s, side, arm)
                if fresh is None:
                    continue
                bk, edge = fresh
            lim = exec_limit(ps, need, bk["asks"][0][0])
            shares = shares_for(lim) if lim else None
            if not shares:
                continue
            if st["cost"] + shares * lim > rp["market_cap_usd"] + 1e-9:
                continue
            if blocked(risk, mode, shares * lim, slot):
                continue
            self._enter(mode, arm, s, side, tok, ps, edge, lim, shares, bk, f, now, slot,
                        st["next_leg"])
            entered += 1
        risk = risk_state(conn, mode, time.time(), arm)
        log.info("%s/%s: %d markets priced, %d entries | today %d orders, open $%.2f, pnl $%+.2f",
                 mode, arm, len(priced), entered, risk["orders_today"], risk["open_usd"],
                 risk["pnl_today"])
        return balance

    def _refresh(self, tok, ps, need, s, side, arm):
        """Live: re-read this token's book just before sending (the cycle's batch is
        seconds old by the time later orders go out - research/race.py) and re-check
        the edge and price range on it.  (book, edge) or None to skip."""
        try:
            bk = fetch_books([tok]).get(tok)
        except Exception as e:
            log.warning("[%s] refresh %s failed: %s", arm, s.series_slug, str(e)[:100])
            return None
        if not bk or not bk["asks"]:
            return None
        ask = bk["asks"][0][0]
        edge = ps - ask - fee_per_share(ask)
        if not (C.MIN_PRICE <= ask <= C.MAX_PRICE) or edge < need:
            log.info("[%s] refresh %s %s: ask now %.3f, edge %.3f - skip", arm, s.series_slug,
                     side, ask, edge)
            return None
        return bk, edge

    def _enter(self, mode, arm, s, side, tok, ps, edge, lim, shares, bk, f, now, slot,
               leg=1) -> None:
        conn = self.conn
        feats = {"xr": f[0], "xn": f[1], "v": f[2], "R": f[3], **{k: self.params.get(k)
                 for k in ("gamma", "b", "sharpen_hours")}}
        ask, size = bk["asks"][0]
        try:
            oid = conn.execute(
                "INSERT INTO trade.orders (created_at, mode, condition_id, series_slug, kind, "
                "strike, side, token_id, target_ts, tau_h, model_p, best_bid, best_ask, ask_size,"
                " edge, limit_price, shares_req, asks, features, slot, arm, leg, status) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending') "
                "RETURNING id",
                (int(now), mode, s.condition_id, s.series_slug, s.kind, s.strike, side, tok,
                 s.target_ts, (s.target_ts - now) / 3600, ps,
                 bk["bids"][0][0] if bk["bids"] else None, ask, size, edge, lim, shares,
                 json.dumps(bk["asks"][:10]), json.dumps(feats), slot, arm, leg)).fetchone()[0]
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
        log.info("%s/%s [%s leg %d] %s %s%s: p=%.3f ask=%.2f edge=%.3f -> %s %.0f @ %s", mode,
                 arm, slot, leg, s.series_slug, side, f" >{s.strike:g}" if s.strike else "", ps, ask, edge, fill.status,
                 fill.shares, f"{fill.avg_price:.3f}" if fill.avg_price else "-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="live authority (row must also say live)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # py-clob-client logs every request
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
