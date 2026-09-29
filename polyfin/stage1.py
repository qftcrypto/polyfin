"""Stage 1: driftless lognormal probability on a variance clock.

    P(yes) = Phi( (ln(S_now / ref) - v/2) / sqrt(v) )

  S_now  latest completed 1m close of the Yahoo underlying
  ref    updown/open: the prior close (price at the previous settlement)
         strikes:     the strike
  v      expected variance from now (or from the reference instant, if that is
         still ahead) to the target instant, from `VarClock`

A market whose reference close is still in the future (tomorrow's up/down,
listed today) has ln(S_now/ref) = 0 by construction - only variance after the
reference counts - so it prices ~0.5 until the reference is set.  Strike
markets price off the distance to the strike from the moment they list.

    python3 -m polyfin.stage1        # price every open market vs its book
"""
from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import OPEN_REF_SERIES
from .data import Bars, load_bars
from .db import DEFAULT_PATH, connect
from .varclock import VarClock, et_date


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_above(x: float, v: float) -> float:
    """P(ln S_T - ln ref > 0) given x = ln(S_now/ref) and variance v, zero drift in price."""
    if v <= 1e-14:
        return 1.0 if x > 0 else 0.0 if x < 0 else 0.5
    return norm_cdf((x - v / 2) / math.sqrt(v))


@dataclass
class Spec:
    """What a market settles on, in the terms of our Yahoo underlying."""
    condition_id: str
    series_slug: str
    kind: str
    symbol: str
    token_yes: str
    target_ts: int            # instant whose price settles the market
    ref_ts: int | None        # instant of the reference close (updown/open)
    strike: float | None
    outcome: float | None     # 1 / 0 / 0.5 once resolved
    end_ts: int


def load_specs(conn) -> list[Spec]:
    """Every market with an underlying, with its target and reference instants."""
    rows = conn.execute(
        "SELECT condition_id, series_slug, kind, symbol, token_yes, event_start, end_ts, "
        "strike, closed, outcome_yes FROM markets WHERE symbol IS NOT NULL "
        "ORDER BY series_slug, end_ts").fetchall()
    ends: dict[str, list[int]] = {}
    for r in rows:
        if r[2] == "updown":
            ends.setdefault(r[1], []).append(r[6])
    specs = []
    for cid, slug, kind, sym, tok, start, end, strike, closed, outcome in rows:
        if kind == "open":
            target = start
            prior = [e for e in ends.get(OPEN_REF_SERIES.get(slug, ""), []) if e < target]
        else:
            target = end
            prior = [e for e in ends.get(slug, []) if e < target]
        ref_ts = max(prior) if prior else None
        if kind != "strikes" and ref_ts is None:
            continue   # first market of the recorded window: prior close unknown
        if kind == "strikes" and strike is None:
            continue
        specs.append(Spec(cid, slug, kind, sym, tok, target, ref_ts, strike,
                          outcome if closed else None, end))
    return specs


class Model:
    def __init__(self, conn):
        self.conn = conn
        self.bars: dict[str, Bars] = {}
        self.clocks: dict[str, VarClock] = {}

    def _get(self, sym: str):
        if sym not in self.bars:
            self.bars[sym] = load_bars(self.conn, sym)
            self.clocks[sym] = VarClock(self.bars[sym])
        return self.bars[sym], self.clocks[sym]

    def reference(self, s: Spec) -> float | None:
        if s.kind == "strikes":
            return s.strike
        return self._get(s.symbol)[0].price_at(s.ref_ts)

    def settle_price(self, s: Spec) -> float | None:
        """Our proxy for the settlement value (open print for `open` markets)."""
        bars = self._get(s.symbol)[0]
        return bars.open_at(s.target_ts) if s.kind == "open" else bars.price_at(s.target_ts)

    def prob(self, s: Spec, t: float, exclude_day=None) -> float | None:
        """P(yes) at time t, or None if the inputs are missing / t is past the target."""
        if t >= s.target_ts:
            return None
        bars, clock = self._get(s.symbol)
        now = bars.price_at(t)
        if now is None:
            return None
        if s.kind != "strikes" and s.ref_ts > t:
            # reference close still ahead: it will be ~S at ref_ts, so only
            # the variance after it matters
            return prob_above(0.0, clock.var(s.ref_ts, s.target_ts, exclude_day))
        ref = self.reference(s)
        if not ref:
            return None
        return prob_above(math.log(now / ref), clock.var(t, s.target_ts, exclude_day))

    @staticmethod
    def exclude_for(s: Spec):
        return et_date(s.target_ts)


def live(conn) -> None:
    now = time.time()
    model = Model(conn)
    books = {r[0]: r[1:] for r in conn.execute(
        "SELECT b.token_id, b.best_bid, b.best_ask, b.ts FROM pm_books b "
        "JOIN (SELECT token_id, MAX(ts) ts FROM pm_books GROUP BY token_id) l "
        "USING (token_id, ts)")}
    rows = []
    for s in load_specs(conn):
        if s.outcome is not None or s.end_ts <= now:
            continue
        p = model.prob(s, now)
        bid, ask, _ = books.get(s.token_yes, (None, None, None))
        label = s.series_slug if s.kind != "strikes" else f"{s.series_slug} >{s.strike:g}"
        edge = None
        if p is not None and bid is not None and ask is not None:
            edge = p - ask if p > ask else p - bid if p < bid else 0.0
        rows.append((s.target_ts, label, p, bid, ask, edge))
    rows.sort()
    print(f"{'target (UTC)':16} {'market':44} {'model':>6} {'bid':>5} {'ask':>5} {'edge':>6}")
    f = lambda x, w: f"{x:{w}.2f}" if x is not None else " " * (w - 1) + "-"
    for ts, label, p, bid, ask, edge in rows:
        t = datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M")
        print(f"{t:16} {label[:44]:44} {f(p, 6)} {f(bid, 5)} {f(ask, 5)} {f(edge, 6)}")
    print("\nedge: model - ask if > 0 (buy yes), model - bid if < 0 (sell yes), 0 inside the spread")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_PATH))
    live(connect(ap.parse_args().db))


if __name__ == "__main__":
    main()
