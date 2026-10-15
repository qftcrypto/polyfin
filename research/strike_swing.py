"""Strikes: buy at edge >= 0.05, SELL on a rise, buy again on a new edge; else hold.

    .venv/bin/python -m research.strike_swing

Points as research/strike_depth.py (strikes, strikes-sharpened model, LODO, every
minute on a recorded book <= 60s old).  $3 per buy (>= 5 shares, <= $5) at
<= ask + 1c on the side with the larger edge (>= 0.05).  While holding:
  ask+X   sell when the ASK of our side >= entry price + X   (the operator's trigger)
  bid+X   sell when the BID of our side >= entry price + X   (locks +X before fees)
  bid>=p  sell when the BID >= the model's own probability   (the market overpays)
A sale hits the bid (Yes bid = 1 - No ask, No bid = 1 - Yes ask), shares capped by
the bid's size, and pays the taker fee.  After a sale the market is flat and may be
bought again (either side) once an edge >= 0.05 reappears, >= 15 min later.  Whatever
is held at the end of the early slot (3h before settlement) is held to resolution.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for

from .fill_repeat import walk
from .strike_repeat import load_books


def build_points():
    from bisect import bisect_right
    from polyfin.db import connect
    from polyfin.stage1 import load_specs
    from polyfin.stage2 import Stage2, collect, evaluate
    conn = connect()
    specs = [s for s in load_specs(conn) if s.kind == "strikes"]
    sp = {s.condition_id: s for s in specs}
    a = collect(Stage2(conn), specs, conn, step=1, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    lad = load_books(conn, list({sp[c].token_yes for c in set(a["cid"])}))
    points = defaultdict(list)
    for i in range(len(a["y"])):
        s = sp[a["cid"][i]]
        t = s.target_ts - a["tau"][i] * 3600
        ts, v = lad.get(s.token_yes, ([], []))
        j = bisect_right(ts, t) - 1
        if j >= 0 and t - ts[j] <= 60:
            points[s.condition_id].append((t, a["tau"][i], a["day"][i], p[i], v[j][0], v[j][1], a["y"][i]))
    for c in points:
        points[c].sort()
    return points


def run(points, rule, x):
    """rule in hold | ask | bid | model; returns per-day P&L and counts."""
    byd = defaultdict(float)
    n_buy = n_sell = 0
    spent = 0.0
    for cid, pts in points.items():
        pos = None                                  # (side, shares, entry px)
        last_buy = -1e18
        day = pts[0][2] if pts else None
        for t, tau, d, py, ya, na, y in pts:
            if tau <= 3:
                break
            day = d
            q = {"yes": py, "no": 1 - py}
            ask = {"yes": ya, "no": na}
            bid = {"yes": [(round(1 - px, 6), z) for px, z in na], "no": [(round(1 - px, 6), z) for px, z in ya]}
            if pos is not None:
                side, sh, px = pos
                if rule == "hold" or not bid[side] or not ask[side]:
                    continue
                b0 = bid[side][0][0]
                hit = ((rule == "ask" and ask[side][0][0] >= px + x - 1e-9) or
                       (rule == "bid" and b0 >= px + x - 1e-9) or
                       (rule == "model" and b0 >= q[side] - 1e-9))
                if hit:
                    sold = min(sh, bid[side][0][1])
                    if sold >= 1:
                        byd[day] += sold * (b0 - fee_per_share(b0))
                        n_sell += 1
                        sh -= sold
                        pos = None if sh < 1 else (side, sh, px)
                continue
            if t - last_buy < 900 or (rule == "hold" and last_buy > -1e18):
                continue
            best = None
            for s_ in ("yes", "no"):
                l = ask[s_]
                if l and 0.05 <= l[0][0] <= 0.97:
                    e = q[s_] - l[0][0] - fee_per_share(l[0][0])
                    if e >= 0.05 and (best is None or e > best[0]):
                        best = (e, s_)
            if best is None:
                continue
            s_ = best[1]
            lim = exec_limit(q[s_], 0.05, ask[s_][0][0])
            n = shares_for(lim) if lim else None
            if not n:
                continue
            got, avg = walk(ask[s_], lim, n)
            if got > 0:
                cost = got * (avg + fee_per_share(avg))
                byd[day] -= cost
                spent += cost
                n_buy += 1
                pos, last_buy = (s_, got, avg), t
        if pos is not None:                         # held to resolution
            side, sh, px = pos
            win = pts[0][6] if side == "yes" else 1 - pts[0][6]
            byd[day] += sh * win
    return byd, n_buy, n_sell, spent


def main() -> None:
    points = build_points()
    days = sorted({q[2] for pts in points.values() for q in pts})
    print(f"strikes: {len(points)} resolved markets with books, {len(days)} days ({days[0]}..{days[-1]})\n")
    print(f"   {'rule':26} {'buys':>5} {'sells':>5} {'spent':>8} {'pnl':>8} {'ret/$':>7} {'pnl/day':>8} {'days+':>6} {'worst day':>9}")
    for name, rule, x in (("hold to resolution (1 buy)", "hold", 0), ("sell ask >= entry + 0.05", "ask", 0.05),
                          ("sell bid >= entry + 0.03", "bid", 0.03), ("sell bid >= entry + 0.05", "bid", 0.05),
                          ("sell bid >= entry + 0.10", "bid", 0.10), ("sell bid >= model p", "model", 0)):
        byd, nb, ns, spent = run(points, rule, x)
        P = sum(byd.values())
        print(f"   {name:26} {nb:5d} {ns:5d} {spent:8.2f} {P:+8.2f} {100 * P / spent:+6.1f}% {P / len(days):+8.2f} "
              f"{sum(v > 0 for v in byd.values())}/{len(byd)} {min(byd.values()):+9.2f}")


if __name__ == "__main__":
    main()
