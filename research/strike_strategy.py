"""WTI strikes: search along the operator's plan (2026-10-17).

    .venv/bin/python -m research.strike_strategy

1  start from the model; 2  buy cheap; 3  a good start time; 4  build the day's book;
5  watch model vs market dynamics and revise / add / hedge.  Per question:
  Q2  return of a first buy by its edge at entry (incl. 0-0.05: 'buy at the ask anyway?')
  Q3  return of a first buy by ET hour and by hours before settlement
  Q5  for a second buy 15-60 min after the first: does it pay more when the MARKET moved
      toward the model since entry (the model being proven right), or away?  And when
      the MODEL moved toward the market (revising itself)?
  Q45 combined rules vs the live rule.
Buys: $3 (>= 5 shares, <= $5) at <= ask + 1c, walking the recorded ladder (BOOKS) or
top of book (SIGNALS).  Returns are per $ after fees, at resolution.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for

from .fill_repeat import walk
from .wti_width import load_moments

ET = ZoneInfo("America/New_York")


def mid_of(ya, na):
    if not ya or not na:
        return None
    b, a = 1 - na[0][0], ya[0][0]
    return (a + b) / 2 if 0 < b <= a < 1 else None


def fill(q, l, th):
    lim = exec_limit(q, max(th, 0.0), l[0][0]) if th > 0 else round(l[0][0] + 0.01, 2)
    n = shares_for(lim) if lim else None
    if not n:
        return None
    got, avg = walk(l, lim, n)
    return (got, avg, got * (avg + fee_per_share(avg))) if got > 0 else None


def series(moments):
    """cid -> chronological [(t, target, k, p_yes, ya, na, y, mid)]"""
    out = defaultdict(list)
    for (target, t), lad in moments.items():
        for k, (cid, p, ya, na, y) in lad.items():
            out[cid].append((t, target, k, p, ya, na, y, mid_of(ya, na)))
    for c in out:
        out[c].sort()
    return out


def first_buys(S, th=0.0):
    """The first moment each market has edge >= th on either side: (cid, t, target, side, edge, ret, cost)."""
    res = []
    for cid, pts in S.items():
        for t, target, k, p, ya, na, y, m in pts:
            if (target - t) / 3600 <= 3:
                break
            best = None
            for side, q, l, w in (("yes", p, ya, y), ("no", 1 - p, na, 1 - y)):
                if l and 0.05 <= l[0][0] <= 0.97:
                    e = q - l[0][0] - fee_per_share(l[0][0])
                    if e >= th and (best is None or e > best[0]):
                        best = (e, side, q, l, w)
            if best:
                f = fill(best[2], best[3], th)
                if f:
                    got, avg, cost = f
                    res.append((cid, t, target, best[1], best[0], got * best[4] - cost, cost))
                break
    return res


def group(title, rows, key, labels):
    print(f"   {title}")
    by = defaultdict(list)
    for r in rows:
        by[key(r)].append(r)
    for lab in labels:
        g = by.get(lab, [])
        if g:
            C, P = sum(r[6] for r in g), sum(r[5] for r in g)
            days = defaultdict(float)
            for r in g:
                days[r[2]] += r[5]
            print(f"      {lab:16} {len(g):4d} buys  ret/$ {100 * P / C:+6.1f}%  pnl {P:+7.2f}  "
                  f"days+ {sum(v > 0 for v in days.values())}/{len(days)}")


def ebin(e):
    for lo, hi in ((0.0, 0.03), (0.03, 0.05), (0.05, 0.08), (0.08, 0.12), (0.12, 0.2), (0.2, 9)):
        if lo <= e < hi:
            return f"{lo:.2f}-{hi:.2f}" if hi < 9 else ">= 0.20"


def hbin(t):
    h = datetime.fromtimestamp(t, ET).hour
    return ["18-24 (evening)", "00-06 (overnight)", "06-10 (morning)", "10-15 (session)"][
        0 if h >= 18 else 1 if h < 6 else 2 if h < 10 else 3] if not (15 <= h < 18) else "15-18"


def taubin(r):
    tau = (r[2] - r[1]) / 3600
    return "24-36h" if tau > 24 else "12-24h" if tau > 12 else "6-12h" if tau > 6 else "3-6h"


def second_buy(S, th, rule, wait=900):
    """Live-like strike2 per market: first buy at edge >= th; ONE second buy >= wait later, same
    side, edge >= th, subject to `rule` on the market's / model's move since the first buy."""
    out = []
    for cid, pts in S.items():
        first = None
        for t, target, k, p, ya, na, y, m in pts:
            if (target - t) / 3600 <= 3:
                break
            if first is None:
                best = None
                for side, q, l, w in (("yes", p, ya, y), ("no", 1 - p, na, 1 - y)):
                    if l and 0.05 <= l[0][0] <= 0.97:
                        e = q - l[0][0] - fee_per_share(l[0][0])
                        if e >= th and (best is None or e > best[0]):
                            best = (e, side, q, l, w)
                if best and m is not None:
                    f = fill(best[2], best[3], th)
                    if f:
                        got, avg, cost = f
                        first = (t, best[1], p, m)
                        out.append((cid, t, target, best[1], best[0], got * best[4] - cost, cost, "first"))
                continue
            if t - first[0] < wait or m is None:
                continue
            t0, side, p0, m0 = first
            q, l, w = (p, ya, y) if side == "yes" else (1 - p, na, 1 - y)
            if not l or not 0.05 <= l[0][0] <= 0.97:
                continue
            e = q - l[0][0] - fee_per_share(l[0][0])
            gap0 = p0 - m0                                    # + = model above market at entry
            mkt_toward = (m - m0) * np.sign(gap0)             # market moved toward the model
            mdl_toward = -(p - p0) * np.sign(gap0)            # model moved toward the market
            ok = {"base": e >= th,
                  "mkt toward": e >= th and mkt_toward > 0.01,
                  "mkt away": e >= th and mkt_toward < -0.01,
                  "model steady": e >= th and mdl_toward <= 0.01,
                  "model revising": e >= th and mdl_toward > 0.01,
                  "never": False}[rule]
            if ok:
                f = fill(q, l, th)
                if f:
                    got, avg, cost = f
                    out.append((cid, t, target, side, e, got * w - cost, cost, "second"))
                break
    return out


def summary(name, rows):
    C, P = sum(r[6] for r in rows), sum(r[5] for r in rows)
    days = defaultdict(float)
    for r in rows:
        days[r[2]] += r[5]
    sec = [r for r in rows if r[7] == "second"]
    Cs, Ps = sum(r[6] for r in sec), sum(r[5] for r in sec)
    w = sorted(days.values())
    print(f"      {name:18} {len(rows):4d} buys ({len(sec):3d} 2nd)  pnl {P:+8.2f} ({100 * P / C if C else 0:+6.1f}%)  "
          f"2nd buys {Ps:+7.2f} ({100 * Ps / Cs if Cs else 0:+6.1f}%)  days+ {sum(v > 0 for v in days.values())}/{len(days)}  "
          f"worst day {w[0] if w else 0:+.2f}")


def run(title, moments):
    S = series(moments)
    print(f"\n== {title}: {len(S)} markets, {len({k[0] for k in moments})} days")
    fb = first_buys(S, 0.0)
    print("   Q2/Q3: first buy per market at the first moment its edge >= 0 (each market once)")
    group("by edge at entry", fb, lambda r: ebin(r[4]),
          ["0.00-0.03", "0.03-0.05", "0.05-0.08", "0.08-0.12", "0.12-0.20", ">= 0.20"])
    fb5 = first_buys(S, 0.05)
    group("by ET hour of entry (first buys at edge >= 0.05)", fb5, lambda r: hbin(r[1]),
          ["18-24 (evening)", "00-06 (overnight)", "06-10 (morning)", "10-15 (session)", "15-18"])
    group("by hours before settlement (edge >= 0.05)", fb5, taubin, ["24-36h", "12-24h", "6-12h", "3-6h"])
    for th in (0.05, 0.08):
        print(f"   Q5: second buy, edge >= {th}, >= 15 min after the first, by what moved since")
        for rule in ("never", "base", "mkt toward", "mkt away", "model steady", "model revising"):
            summary(rule, second_buy(S, th, rule))


def main() -> None:
    books, sig = load_moments()
    run("BOOKS (LODO, recorded ladders)", books)
    run("SIGNALS (forward, real time)", sig)


if __name__ == "__main__":
    main()
