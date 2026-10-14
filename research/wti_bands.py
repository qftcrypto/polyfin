"""WTI strikes: buy a BAND of $1 buckets [K, K+w) when the model prices it above the market.

    .venv/bin/python -m research.wti_bands

A band = Yes(above K) + No(above K+w): the pair costs c (asks + fees) and pays 2
if the close lands in [K, K+w), else 1 - so its price is c - 1 and the model's
edge is P_model(K) - P_model(K+w) - (c - 1).  Two legs = two spreads and two fees.
Per settlement day, the band is bought (5 shares each leg, walking the recorded
ladder up to ask + 1c) at the first moment its edge >= 0.05, then once more >= 15
min later if the same band still has edge >= 0.05 (strike2's 2 buys).
  centred  the band of width w whose middle is nearest the model's 0.5 point
  best     the band of width w with the largest edge at that moment
Sources as research/wti_width.py: BOOKS (LODO model, recorded books, 11 days) and
SIGNALS (the bot's real-time model and book, forward).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from polyfin.live.fees import fee_per_share

from .fill_repeat import walk
from .wti_width import centre, load_moments

ET = ZoneInfo("America/New_York")
SHARES = 5


def bands(lad, w):
    ks = sorted(lad)
    out = []
    for i, k in enumerate(ks):
        k2 = k + w
        if k2 not in lad:
            continue
        (_, p1, ya, _, y1), (_, p2, _, na, y2) = lad[k], lad[k2]
        if not ya or not na or not (0.01 <= ya[0][0] <= 0.99 and 0.01 <= na[0][0] <= 0.99):
            continue
        c = ya[0][0] + na[0][0] + fee_per_share(ya[0][0]) + fee_per_share(na[0][0])
        out.append((k, p1 - p2 - (c - 1), ya, na, y1, y2))
    return out


def run(moments, w, how):
    held = {}                                       # target -> [K, last_t, n]
    trades = []
    for target, t in sorted(moments, key=lambda k: k[1]):
        lad = moments[(target, t)]
        h = held.get(target)
        if h and (h[2] >= 2 or t - h[1] < 900):
            continue
        bs = bands(lad, w)
        if not bs:
            continue
        if h:
            cand = [b for b in bs if b[0] == h[0]]
        elif how == "best":
            cand = [max(bs, key=lambda b: b[1])]
        else:
            ks = sorted(lad)
            cp = centre(ks, [lad[k][1] for k in ks])
            if cp is None:
                continue
            cand = [min(bs, key=lambda b: abs(b[0] + w / 2 - cp))]
        if not cand or cand[0][1] < 0.05:
            continue
        k, e, ya, na, y1, y2 = cand[0]
        g1, a1 = walk(ya, ya[0][0] + 0.01, SHARES)
        g2, a2 = walk(na, na[0][0] + 0.01, SHARES)
        n = min(g1, g2)
        if n < SHARES:
            continue
        cost = n * (a1 + fee_per_share(a1) + a2 + fee_per_share(a2))
        pay = n * (y1 + (1 - y2))                      # 2 in the band, 1 outside
        held[target] = [k, t, (h[2] if h else 0) + 1]
        trades.append((target, cost, pay - cost, int(y1 == 1 and y2 == 0)))
    return trades


def report(title, moments):
    print(f"\n== {title}")
    print(f"   {'band':14} {'buys':>4} {'in band':>7} {'cost':>8} {'pnl':>8} {'ret/$':>7} {'days+':>6} {'worst day':>9}   by day")
    for how in ("centred", "best"):
        for w in (1, 2, 3, 4, 5):
            T = run(moments, w, how)
            if not T:
                print(f"   {how} ${w}-wide     -")
                continue
            byd = defaultdict(float)
            for tg, c, pl, hit in T:
                byd[tg] += pl
            C, P = sum(x[1] for x in T), sum(x[2] for x in T)
            print(f"   {how:7} ${w}-wide {len(T):4d} {100 * np.mean([x[3] for x in T]):6.0f}% {C:8.2f} {P:+8.2f} "
                  f"{100 * P / C:+6.1f}% {sum(v > 0 for v in byd.values())}/{len(byd)} {min(byd.values()):+9.2f}   "
                  + " ".join(f"{datetime.fromtimestamp(d, ET):%m-%d}:{v:+.0f}" for d, v in sorted(byd.items())))


def main() -> None:
    books, sig = load_moments()
    report(f"BOOKS: WTI, LODO model, recorded books ({len({k[0] for k in books})} days)", books)
    report(f"SIGNALS: forward, the bot's real-time model ({len({k[0] for k in sig})} days)", sig)


if __name__ == "__main__":
    main()
