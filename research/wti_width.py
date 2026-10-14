"""WTI strikes: trade the model's WIDTH at the market's CENTRE.

    .venv/bin/python -m research.wti_width

Our strikes edge is the distribution's width (strike_b: realized moves ~0.86x the
model's sd; the market prices it too wide).  Its centre comes from Yahoo CL=F,
~10 min late, and where the model's centre disagrees with the market's, the book
becomes a directional bet (10/12).  'width-only': at each moment the model's
P(above K) curve across the ladder is shifted horizontally so that its 0.5 point
sits at the MARKET's 0.5 point (book mids); edges are then taken from the shifted
curve.  The same strike2 rule (2 buys at edge >= 0.05, >= 15 min apart, same side,
$3 / >= 5 shares / <= $5, <= ask + 1c) runs with each probability.

Two sources:  BOOKS  stage 2 LODO (strike_b) on recorded books, every minute;
              SIGNALS  the bot's own real-time model_p and book (trade.signals,
              from 2026-10-08) - forward, with live data delays.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict

import numpy as np

from polyfin.db import connect
from polyfin.live.fees import fee_per_share
from polyfin.live.sizing import exec_limit, shares_for
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate

from .fill_repeat import walk
from .strike_repeat import load_books

SERIES = "wti-daily-close-uo"


def centre(ks, ps):
    """Strike where a decreasing P(above K) curve crosses 0.5 (linear interpolation)."""
    for i in range(len(ks) - 1):
        a, b = ps[i], ps[i + 1]
        if a >= 0.5 >= b and a != b:
            return ks[i] + (a - 0.5) / (a - b) * (ks[i + 1] - ks[i])
    return None


def shifted(ks, ps, shift):
    """P(above K) of the model curve moved right by `shift` dollars: p'(K) = p(K - shift)."""
    x = np.array(ks) - shift
    return list(np.interp(ks, x, ps, left=ps[0], right=ps[-1]))


def simulate(moments, use):
    """moments[(target, t)] = {strike: (cid, p_model, yes_ladder, no_ladder, y)};
    use(ks, ps, mids) -> probabilities to trade on (None = skip the moment)."""
    state = {}                                       # cid -> [side, last_t, n]
    fills = []
    for (target, t) in sorted(moments, key=lambda k: k[1]):
        lad = moments[(target, t)]
        ks = sorted(lad)
        if len(ks) < 3:
            continue
        mids = [((lad[k][2][0][0] if lad[k][2] else None), (1 - lad[k][3][0][0] if lad[k][3] else None)) for k in ks]
        mids = [(a + b) / 2 if a is not None and b is not None else None for a, b in mids]
        ps = use(ks, [lad[k][1] for k in ks], mids)
        if ps is None:
            continue
        for k, q in zip(ks, ps):
            cid, _, ya, na, y = lad[k]
            st = state.setdefault(cid, [None, -1e18, 0])
            if st[2] >= 2 or t - st[1] < 900:
                continue
            best = None
            for side, qs, l, w in (("yes", q, ya, y), ("no", 1 - q, na, 1 - y)):
                if st[0] not in (None, side) or not l or not 0.05 <= l[0][0] <= 0.97:
                    continue
                e = qs - l[0][0] - fee_per_share(l[0][0])
                if e >= 0.05 and (best is None or e > best[0]):
                    best = (e, side, qs, l, w)
            if best is None:
                continue
            e, side, qs, l, w = best
            lim = exec_limit(qs, 0.05, l[0][0])
            n = shares_for(lim) if lim else None
            if not n:
                continue
            got, avg = walk(l, lim, n)
            if got > 0:
                st[:] = [side, t, st[2] + 1]
                fills.append((cid, target, k, side, got, avg, got * (avg + fee_per_share(avg)), w))
    return fills


def show(name, fills):
    byd = defaultdict(float)
    worst = defaultdict(float)
    for cid, target, k, side, got, avg, cost, w in fills:
        byd[target] += got * w - cost
    C = sum(f[6] for f in fills)
    P = sum(byd.values())
    print(f"   {name:34} {len(fills):4d} buys  cost ${C:7.2f}  pnl {P:+8.2f} ({100 * P / C if C else 0:+6.1f}%)  "
          f"days+ {sum(v > 0 for v in byd.values())}/{len(byd)}  worst day {min(byd.values()) if byd else 0:+7.2f}")
    return byd


def report(title, moments):
    raw = lambda ks, ps, mids: ps

    def width(ks, ps, mids):
        km = [(k, m) for k, m in zip(ks, mids) if m is not None]
        if len(km) < 3:
            return None
        cm = centre([k for k, _ in km], [m for _, m in km])
        cp = centre(ks, ps)
        if cm is None or cp is None:
            return None
        return shifted(ks, ps, cm - cp)

    def agree(tol):                     # trade the raw model only when the centres agree within tol $
        def f(ks, ps, mids):
            km = [(k, m) for k, m in zip(ks, mids) if m is not None]
            if len(km) < 3:
                return None
            cm, cp = centre([k for k, _ in km], [m for _, m in km]), centre(ks, ps)
            return ps if cm is not None and cp is not None and abs(cm - cp) <= tol else None
        return f
    print(f"\n== {title}")
    a = show("current (model centre)", simulate(moments, raw))
    b = show("width-only (market centre)", simulate(moments, width))
    for tol in (0.25, 0.50, 0.75, 1.00, 1.50):
        show(f"current, centres agree <= ${tol:.2f}", simulate(moments, agree(tol)))
    days = sorted(set(a) | set(b))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    f = lambda t: datetime.fromtimestamp(t, ZoneInfo("America/New_York")).strftime("%m-%d")
    print("   by day  current:   " + " ".join(f"{f(d)}:{a.get(d, 0):+.0f}" for d in days))
    print("           width-only:" + " ".join(f"{f(d)}:{b.get(d, 0):+.0f}" for d in days))


def load_moments():
    """(BOOKS moments, SIGNALS moments): {(target_ts, t): {strike: (cid, p, yes_ladder, no_ladder, y)}}."""
    conn = connect()
    specs = [s for s in load_specs(conn) if s.series_slug == SERIES and s.outcome is not None]
    sp = {s.condition_id: s for s in specs}
    # BOOKS: LODO model every minute on recorded books
    a = collect(Stage2(conn), specs, conn, step=1, hours=36, min_volume=0)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    lad = load_books(conn, [s.token_yes for s in specs])
    moments = defaultdict(dict)
    for i in range(len(a["y"])):
        s = sp[a["cid"][i]]
        if a["tau"][i] <= 3:
            continue
        t = int(round(s.target_ts - a["tau"][i] * 3600))
        ts, v = lad.get(s.token_yes, ([], []))
        j = bisect_right(ts, t) - 1
        if j >= 0 and t - ts[j] <= 60:
            moments[(s.target_ts, t // 60 * 60)][s.strike] = (s.condition_id, p[i], v[j][0], v[j][1], a["y"][i])
    # SIGNALS: the bot's real-time model and the book it fetched (top of book only)
    rows = conn.execute(
        "SELECT s.ts, s.condition_id, s.model_p, s.yes_bid, s.yes_bid_size, s.yes_ask, s.yes_ask_size, "
        "s.no_ask, s.no_ask_size FROM trade.signals s JOIN markets m USING (condition_id) "
        "WHERE m.series_slug = %s AND m.outcome_yes IS NOT NULL AND s.tau_h > 3", (SERIES,)).fetchall()
    conn.commit()
    sig = defaultdict(dict)
    for ts, cid, mp, yb, ybs, ya, yas, na, nas in rows:
        s = sp.get(cid)
        if s is None or ya is None or na is None:
            continue
        sig[(s.target_ts, ts // 30 * 30)][s.strike] = (cid, mp, [(ya, yas or 0)], [(na, nas or 0)], s.outcome)
    return moments, sig


def main() -> None:
    moments, sig = load_moments()
    report(f"BOOKS: WTI strikes, LODO model, recorded books ({len({k[0] for k in moments})} days)", moments)
    report(f"SIGNALS: forward, real-time model ({len({k[0] for k in sig})} days)", sig)


if __name__ == "__main__":
    main()
