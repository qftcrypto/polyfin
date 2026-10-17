"""Strikes edge-threshold sweep for the strike2 / strike_rep rules (WTI ladder).

    .venv/bin/python -m research.strike_threshold

Thresholds 0.02 / 0.03 / 0.05 (live) / 0.08 / 0.10 on the first AND every repeat
buy (edge = p - ask - fee), $3 per buy (>= 5 shares, <= $5), <= ask + 1c, >= 15
min between buys of one market, same side; strike2 = <= 2 buys, strike_rep = <= 10
buys and <= $30 per market.  Two sources (research/wti_width.load_moments):
BOOKS (LODO model, recorded ladders, all settled days) and SIGNALS (the bot's
real-time model and book - forward, with live delays).  Accept a threshold only if
it holds on BOTH, not the best cell.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for

from .fill_repeat import walk
from .wti_width import load_moments

ET = ZoneInfo("America/New_York")
THRESHOLDS = (0.02, 0.03, 0.05, 0.08, 0.10)
RULES = (("strike2", 2, 10.0), ("strike_rep", 10, 30.0))


def simulate(moments, th, max_buys, cap):
    state, fills = {}, []
    for target, t in sorted(moments, key=lambda k: k[1]):
        for k, (cid, q, ya, na, y) in moments[(target, t)].items():
            st = state.setdefault(cid, [None, -1e18, 0, 0.0])
            if st[2] >= max_buys or t - st[1] < 900:
                continue
            best = None
            for side, qs, l, w in (("yes", q, ya, y), ("no", 1 - q, na, 1 - y)):
                if st[0] not in (None, side) or not l or not 0.05 <= l[0][0] <= 0.97:
                    continue
                e = qs - l[0][0] - fee_per_share(l[0][0])
                if e >= th and (best is None or e > best[0]):
                    best = (e, side, qs, l, w)
            if best is None:
                continue
            e, side, qs, l, w = best
            lim = exec_limit(qs, th, l[0][0])
            n = shares_for(lim) if lim else None
            if not n or st[3] + n * lim > cap + 1e-9:
                continue
            got, avg = walk(l, lim, n)
            if got > 0:
                cost = got * (avg + fee_per_share(avg))
                st[:] = [side, t, st[2] + 1, st[3] + cost]
                fills.append((cid, target, cost, got * w - cost))
    return fills


def report(title, moments):
    days = sorted({k[0] for k in moments})
    print(f"\n== {title} ({len(days)} settlement days)")
    print(f"   {'rule':11} {'edge>=':>6} {'buys':>5} {'cost':>8} {'pnl':>8} {'ret/$':>7} {'days+':>6} {'w/o best5':>9} {'worst day':>9}   by day")
    for name, mb, cap in RULES:
        for th in THRESHOLDS:
            F = simulate(moments, th, mb, cap)
            if not F:
                print(f"   {name:11} {th:6.2f}     -")
                continue
            byd, bym = defaultdict(float), defaultdict(float)
            for cid, tg, c, pl in F:
                byd[tg] += pl
                bym[cid] += pl
            C, P = sum(f[2] for f in F), sum(f[3] for f in F)
            print(f"   {name:11} {th:6.2f} {len(F):5d} {C:8.2f} {P:+8.2f} {100 * P / C:+6.1f}% "
                  f"{sum(v > 0 for v in byd.values())}/{len(byd)} {P - sum(sorted(bym.values(), reverse=True)[:5]):+9.2f} "
                  f"{min(byd.values()):+9.2f}   " + " ".join(
                      f"{datetime.fromtimestamp(d, ET):%m-%d}:{v:+.0f}" for d, v in sorted(byd.items())))


def main() -> None:
    books, sig = load_moments()
    report("BOOKS: WTI strikes, LODO model, recorded books", books)
    report("SIGNALS: forward, the bot's real-time model and book", sig)


if __name__ == "__main__":
    main()
