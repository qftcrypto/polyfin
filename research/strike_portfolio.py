"""WTI strikes: portfolio-aware buying on top of strike2 (2026-10-17).

    .venv/bin/python -m research.strike_portfolio

The day's book is one bet on the close.  At each moment the bot sees its holdings'
P&L in every close bucket of the ladder and the model's bucket probabilities.
  base       strike2: 2 buys/market at edge >= th, >= 15 min apart, same side, $3
  cap X      (a) skip a buy that would put the day's worst case below -$X
  risk l     (b) take a buy only if dEV - l * d(sd) >= 0 (model probabilities)
  hedge e    (c) base, plus: when the worst case is below -$20, up to 2 hedge buys/day
             (5 shares) - the buy that most raises the worst case, if its edge >= e
Sources: BOOKS (LODO model on recorded ladders) and SIGNALS (the bot's real-time
model and book; top of book only).  As research/strike_threshold.py.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for

from .fill_repeat import walk
from .wti_width import load_moments

ET = ZoneInfo("America/New_York")


def scen(ks, ps):
    """Representative closes and model probabilities per bucket of a ladder."""
    ps = np.minimum.accumulate(np.clip(ps, 0, 1))             # P(above K) must not rise with K
    reps = [ks[0] - 0.5] + [k + 0.5 for k in ks]
    pr = [1 - ps[0]] + [ps[i] - ps[i + 1] for i in range(len(ks) - 1)] + [ps[-1]]
    return np.array(reps), np.maximum(np.array(pr), 0)


def payoff(pos, reps):
    v = np.zeros(len(reps))
    for k, side, sh, cost in pos:
        v += sh * ((reps > k) if side == "yes" else (reps <= k)) - cost
    return v


def simulate(moments, th, rule, x):
    held = defaultdict(list)                                  # target -> [(k, side, sh, cost)]
    state, hedges, out = {}, defaultdict(int), []
    for target, t in sorted(moments, key=lambda k: k[1]):
        lad = moments[(target, t)]
        ks = sorted(lad)
        if len(ks) < 3:
            continue
        reps, pr = scen(ks, [lad[k][1] for k in ks])
        cands = []
        for k in ks:
            cid, q, ya, na, y = lad[k]
            st = state.setdefault(cid, [None, -1e18, 0])
            for side, qs, l, w in (("yes", q, ya, y), ("no", 1 - q, na, 1 - y)):
                if not l or not 0.05 <= l[0][0] <= 0.97:
                    continue
                e = qs - l[0][0] - fee_per_share(l[0][0])
                cands.append((e, k, cid, side, qs, l, w, st))
        cur = payoff(held[target], reps)

        def buy(c, n=None):
            e, k, cid, side, qs, l, w, st = c
            lim = exec_limit(qs, min(th, e), l[0][0]) if n is None else round(l[0][0] + 0.01, 2)
            n = n or (shares_for(lim) if lim else None)
            if not n:
                return None
            got, avg = walk(l, lim, n)
            return (got, avg) if got > 0 else None

        for c in sorted(cands, key=lambda c: -c[0]):
            e, k, cid, side, qs, l, w, st = c
            if e < th or st[2] >= 2 or t - st[1] < 900 or st[0] not in (None, side):
                continue
            ask = l[0][0]
            n = shares_for(exec_limit(qs, th, ask) or ask)
            if not n:
                continue
            add = n * ((reps > k) if side == "yes" else (reps <= k)) - n * (ask + fee_per_share(ask))
            new = cur + add
            if rule == "cap" and new.min() < -x:
                continue
            if rule == "risk":
                sd0 = math.sqrt(max((pr * cur ** 2).sum() - (pr * cur).sum() ** 2, 0))
                sd1 = math.sqrt(max((pr * new ** 2).sum() - (pr * new).sum() ** 2, 0))
                if (pr * add).sum() - x * (sd1 - sd0) < 0:
                    continue
            f = buy(c)
            if f:
                got, avg = f
                cost = got * (avg + fee_per_share(avg))
                held[target].append((k, side, got, cost))
                st[:] = [side, t, st[2] + 1]
                out.append((target, cost, got * w - cost, "base"))
                cur = payoff(held[target], reps)
        if rule == "hedge" and cur.min() < -20 and hedges[target] < 2:
            best = None
            for c in cands:
                e, k, cid, side, qs, l, w, st = c
                if e < x:
                    continue
                ask = l[0][0]
                add = 5 * ((reps > k) if side == "yes" else (reps <= k)) - 5 * (ask + fee_per_share(ask))
                gain = (cur + add).min() - cur.min()
                if gain > 0.5 and (best is None or gain > best[0]):
                    best = (gain, c)
            if best:
                f = buy(best[1], 5)
                if f:
                    got, avg = f
                    _, k, cid, side, qs, l, w, st = best[1]
                    cost = got * (avg + fee_per_share(avg))
                    held[target].append((k, side, got, cost))
                    hedges[target] += 1
                    out.append((target, cost, got * w - cost, "hedge"))
    return out


def report(title, moments):
    print(f"\n== {title} ({len({k[0] for k in moments})} days)")
    print(f"   {'variant':16} {'buys':>5} {'cost':>8} {'pnl':>8} {'ret/$':>7} {'days+':>6} {'worst day':>9} {'2nd worst':>9}   by day")
    for th in (0.08, 0.05):
        print(f"   -- edge >= {th}")
        for name, rule, x in (("base", None, 0), ("(a) cap $10", "cap", 10), ("(a) cap $20", "cap", 20),
                              ("(a) cap $30", "cap", 30), ("(b) risk 0.25", "risk", 0.25),
                              ("(b) risk 0.5", "risk", 0.5), ("(b) risk 1.0", "risk", 1.0),
                              ("(c) hedge e>=-0.02", "hedge", -0.02), ("(c) hedge e>=-0.05", "hedge", -0.05)):
            F = simulate(moments, th, rule, x)
            byd = defaultdict(float)
            for tg, c, pl, kind in F:
                byd[tg] += pl
            C, P = sum(f[1] for f in F), sum(f[2] for f in F)
            w = sorted(byd.values())
            print(f"   {name:16} {len(F):5d} {C:8.2f} {P:+8.2f} {100 * P / C if C else 0:+6.1f}% "
                  f"{sum(v > 0 for v in byd.values())}/{len(byd)} {w[0]:+9.2f} {w[1] if len(w) > 1 else 0:+9.2f}   "
                  + " ".join(f"{datetime.fromtimestamp(d, ET):%m-%d}:{v:+.0f}" for d, v in sorted(byd.items())))


def main() -> None:
    books, sig = load_moments()
    report("BOOKS: WTI strikes, LODO model (no vol regime), recorded ladders", books)
    report("SIGNALS: forward, the bot's real-time model and book", sig)


if __name__ == "__main__":
    main()
