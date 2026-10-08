"""Up/down: what divergence D pays, given the model is worse than the market there.

    .venv/bin/python -m research.ud_threshold

1. Shrinkage: on real-book points, regress (outcome - book mid) on (model - book mid).
   lambda = the share of a disagreement that is real.  At the ask the true edge is
   ~ lambda * D_mid - (ask - mid) - fee, so the break-even D_mid = cost / lambda.
2. Real-book backtest: first buy per market at edge-to-ask >= D, $1 per contract.
3. Forward: actual paper + live up/down early fills by entry edge.
CIs bootstrap MARKETS (points within a market are not independent).
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .zones import book_index

RNG = np.random.default_rng(20261008)


def boot_markets(cids, f, B=2000):
    u = np.unique(cids)
    idx = {c: np.flatnonzero(cids == c) for c in u}
    out = []
    for _ in range(B):
        pick = RNG.choice(u, len(u))
        out.append(f(np.concatenate([idx[c] for c in pick])))
    return np.percentile(out, [5, 95])


def main() -> None:
    conn = connect()
    specs = [s for s in load_specs(conn) if s.kind == "updown"]
    sp = {s.condition_id: s for s in specs}
    a = collect(Stage2(conn), specs, conn, step=5, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    bi = book_index(conn)
    y, tau, day, cid = a["y"], a["tau"], a["day"], a["cid"]
    bid, ask = np.full(len(y), np.nan), np.full(len(y), np.nan)
    for i in range(len(y)):
        s = sp[cid[i]]
        t = s.target_ts - tau[i] * 3600
        bt, bv = bi.get(s.token_yes, ([], []))
        j = bisect_right(bt, t) - 1
        if j >= 0 and t - bt[j] <= 120:
            b, k = bv[j]
            if b is not None and k is not None and 0 < b <= k < 1 and k - b <= 0.10:
                bid[i], ask[i] = b, k
    ok = ~np.isnan(bid) & (tau > 3)
    mid = (bid + ask) / 2
    print(f"up/down, early slot (>3h), real books: {ok.sum()} points, {len(set(cid[ok]))} markets, "
          f"{len(set(day[ok]))} days; median half-spread {np.median((ask - bid)[ok] / 2):.3f}\n")

    # 1. shrinkage, folded to the side the model favours vs the market
    d = (p - mid)[ok]
    r = (y - mid)[ok]
    c_ok = cid[ok]
    lam = lambda ix: float((d[ix] @ r[ix]) / (d[ix] @ d[ix]))
    lo, hi = boot_markets(c_ok, lam)
    print(f"== 1. how much of a disagreement is real: lambda = {lam(np.arange(len(d))):+.2f} "
          f"(90% CI over markets {lo:+.2f}..{hi:+.2f}; 1 = model right, 0 = market right)")
    print(f"   {'|model - mid|':14} {'points':>7} {'mkts':>5} {'avg D':>6} {'realized':>9} {'share real':>10}")
    for lo_, hi_ in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 1.0)):
        g = (np.abs(d) >= lo_) & (np.abs(d) < hi_)
        if g.sum() < 50:
            continue
        sg = np.sign(d[g])
        print(f"   {lo_:.2f}-{hi_:.2f}      {g.sum():7d} {len(set(c_ok[g])):5d} {np.abs(d[g]).mean():6.3f} "
              f"{(sg * r[g]).mean():+9.3f} {(sg * r[g]).mean() / np.abs(d[g]).mean():+10.2f}")

    # 2. backtest by threshold (edge to ask, after fee), first buy per market
    print("\n== 2. real-book backtest: first buy per market at edge-to-ask >= D ($1 contracts)")
    print(f"   {'D':>5} {'buys':>5} {'win':>5} {'avg ask':>7} {'ret/$':>7} {'90% CI':>16} {'days+':>6} {'w/o best5':>9}")
    rows = []
    for i in np.flatnonzero(ok):
        for side, q, k, w in (("yes", p[i], ask[i], y[i]), ("no", 1 - p[i], 1 - bid[i], 1 - y[i])):
            if 0.05 <= k <= 0.97:
                rows.append((cid[i], day[i], tau[i], q - k - fee_per_share(k), k, w))
    rows.sort(key=lambda r: -r[2])
    for D in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40):
        first = {}
        for r_ in rows:
            if r_[0] not in first and r_[3] >= D:
                first[r_[0]] = r_
        t = list(first.values())
        if len(t) < 5:
            continue
        cost = np.array([r_[4] + fee_per_share(r_[4]) for r_ in t])
        pl = np.array([r_[5] for r_ in t]) - cost
        cc = np.array([r_[0] for r_ in t])
        lo_, hi_ = boot_markets(cc, lambda ix: pl[ix].sum() / cost[ix].sum())
        byd = defaultdict(float)
        for r_, x in zip(t, pl):
            byd[r_[1]] += x
        print(f"   {D:5.2f} {len(t):5d} {100 * np.mean([r_[5] for r_ in t]):4.0f}% {np.mean([r_[4] for r_ in t]):7.3f} "
              f"{100 * pl.sum() / cost.sum():+6.1f}% {100 * lo_:+7.1f}..{100 * hi_:+5.1f}% "
              f"{sum(v > 0 for v in byd.values())}/{len(byd)} {pl.sum() - np.sort(pl)[::-1][:5].sum():+9.2f}")

    # 3. forward fills
    print("\n== 3. forward: actual up/down early fills (live + all paper arms), by entry edge")
    q = conn.execute(
        "SELECT edge, best_ask, shares_filled * avg_price + fee, pnl, condition_id, mode FROM trade.orders "
        "WHERE shares_filled > 0 AND pnl IS NOT NULL AND slot = 'early' AND kind = 'updown'").fetchall()
    conn.commit()
    print(f"   {'entry edge':11} {'fills':>5} {'mkts':>5} {'cost':>8} {'pnl':>8} {'ret/$':>7}   live only")
    for lo_, hi_ in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 1.0)):
        g = [x for x in q if x[0] is not None and lo_ <= x[0] < hi_]
        lv = [x for x in g if x[5] == "live"]
        if g:
            c, pl = sum(x[2] for x in g), sum(x[3] for x in g)
            ls = (f"{len(lv)} fills {100 * sum(x[3] for x in lv) / sum(x[2] for x in lv):+.1f}%" if lv else "-")
            print(f"   {lo_:.2f}-{hi_:.2f}   {len(g):5d} {len({x[4] for x in g}):5d} {c:8.2f} {pl:+8.2f} "
                  f"{100 * pl / c:+6.1f}%   {ls}")


if __name__ == "__main__":
    main()
