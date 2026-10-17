"""WTI strikes: should a QUIET recent past narrow the model?  (2026-10-17)

    .venv/bin/python -m research.strike_regime

Stage 2 scales the remaining variance by R^gamma, R = realized / normal variance
over the last 6h (clipped 0.25..4).  On 10/16 a quiet overnight put R at its 0.25
floor, the model priced ~$1.1-1.2 of movement against the market's ~$1.6-2.0, and
oil moved +$3.  Variants (gamma and strike_b refitted leave-one-day-out each time):
  current   v * R^gamma                 (live)
  up-only   v * max(R, 1)^gamma         calm never narrows, turbulence still widens
  none      v                           no regime scaling
BOOKS: LODO on recorded books (Brier vs book mid where spread <= 0.10; strike2 at
edge 0.05 / 0.08).  SIGNALS: the bot's real-time probabilities re-scaled to each
variant (d' = d * (b'/b) * sqrt(R^gamma / g(R)^gamma'), R recomputed at the moment).
"""
from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

from polyfin.db import connect
from polyfin.stage1 import load_specs
from polyfin.stage2 import (R_CLIP, VOL_WINDOW, Stage2, collect, evaluate, fit_sharp,
                            fit_strike_b, strike_rows)

from .strike_repeat import load_books
from .strike_threshold import simulate
from .wti_width import SERIES

ET = ZoneInfo("America/New_York")
N = NormalDist()
VARIANTS = {"current": lambda R: R, "up-only": lambda R: np.maximum(R, 1.0), "none": lambda R: np.ones_like(R)}
LIVE_GAMMA, LIVE_B = 0.5, 1.3


def day_of(ts):
    return datetime.fromtimestamp(ts, ET).strftime("%m-%d")


def table(title, rows, days):
    """rows[variant] = (p array, y, mid, day labels, mask)"""
    print(f"\n== {title}: Brier where we trade (early, spread <= 0.10); lower is better")
    print(f"   {'variant':9} {'model':>7} {'market':>7} {'gap':>8}   per day gap (model - market)")
    for v, (p, y, mid, dl, ok) in rows.items():
        B = lambda q, g: ((q[g] - y[g]) ** 2).mean()
        print(f"   {v:9} {B(p, ok):7.4f} {B(mid, ok):7.4f} {B(p, ok) - B(mid, ok):+8.4f}   " + " ".join(
            f"{d}:{B(p, ok & (dl == d)) - B(mid, ok & (dl == d)):+.4f}" for d in days if (ok & (dl == d)).any()))


def trading(title, moments_by_variant):
    print(f"\n== {title}: strike2 (2 buys, 15 min apart, $3)")
    for th in (0.05, 0.08):
        for v, mom in moments_by_variant.items():
            F = simulate(mom, th, 2, 10.0)
            byd = defaultdict(float)
            for cid, tg, c, pl in F:
                byd[tg] += pl
            C, P = sum(f[2] for f in F), sum(f[3] for f in F)
            print(f"   edge>={th:.2f} {v:9} {len(F):4d} buys  pnl {P:+8.2f} ({100 * P / C if C else 0:+6.1f}%)  "
                  f"days+ {sum(x > 0 for x in byd.values())}/{len(byd)}  " +
                  " ".join(f"{day_of(d)}:{x:+.0f}" for d, x in sorted(byd.items())))


def main() -> None:
    conn = connect()
    specs = [s for s in load_specs(conn) if s.series_slug == SERIES and s.outcome is not None]
    sp = {s.condition_id: s for s in specs}
    a = collect(Stage2(conn), specs, conn, step=1, hours=36, min_volume=0)
    lad = load_books(conn, [s.token_yes for s in specs])
    early = a["tau"] > 3
    mid = np.full(len(a["y"]), np.nan)
    book_at = []
    for i in range(len(a["y"])):
        s = sp[a["cid"][i]]
        t = int(round(s.target_ts - a["tau"][i] * 3600))
        ts, v = lad.get(s.token_yes, ([], []))
        j = bisect_right(ts, t) - 1
        if j >= 0 and t - ts[j] <= 60:
            ya, na = v[j]
            if ya and na:
                bid, ask = 1 - na[0][0], ya[0][0]
                if 0 < bid <= ask < 1 and ask - bid <= 0.10:
                    mid[i] = (bid + ask) / 2
            book_at.append((i, t, ya, na))
    dl = np.array([day_of(sp[c].target_ts) for c in a["cid"]])
    days = sorted(set(dl))
    ok = early & ~np.isnan(mid)
    rows, mom, fits = {}, {}, {}
    for v, g in VARIANTS.items():
        a2 = dict(a, R=g(a["R"]))
        out, _ = evaluate(a2)
        p = out["sharp<=3h"]
        gam = fit_sharp(a2, np.ones(len(a2["y"]), bool), fit_b=False)["gamma"]
        fits[v] = (gam, float(fit_strike_b(a2, strike_rows(a2), gam)))
        rows[v] = (p, a["y"], mid, dl, ok)
        m = defaultdict(dict)
        for i, t, ya, na in book_at:
            if early[i]:
                s = sp[a["cid"][i]]
                m[(s.target_ts, t // 60 * 60)][s.strike] = (s.condition_id, p[i], ya, na, a["y"][i])
        mom[v] = m
    print("full-data fits (gamma, strike_b): " + ", ".join(f"{v} ({g:.2f}, {b:.2f})" for v, (g, b) in fits.items()))
    table("BOOKS (LODO)", rows, days)
    trading("BOOKS (LODO)", mom)

    # SIGNALS: re-scale the bot's real-time probabilities to each variant
    model = Stage2(conn)
    clock = model.m1._get("CL=F")[1]
    sig = conn.execute(
        "SELECT s.ts, s.condition_id, s.model_p, s.yes_bid, s.yes_ask, s.yes_ask_size, s.no_ask, s.no_ask_size "
        "FROM trade.signals s JOIN markets m USING (condition_id) "
        "WHERE m.series_slug = %s AND m.outcome_yes IS NOT NULL AND s.tau_h > 3", (SERIES,)).fetchall()
    conn.commit()
    Rcache = {}

    def R_at(ts):
        k = ts // 300
        if k not in Rcache:
            rv, ev = clock.realized(ts - VOL_WINDOW, ts)
            Rcache[k] = min(max(rv / ev, R_CLIP[0]), R_CLIP[1]) if ev > 0 and rv > 0 else 1.0
        return Rcache[k]
    srows = {v: ([], [], [], [], []) for v in VARIANTS}
    smom = {v: defaultdict(dict) for v in VARIANTS}
    for ts, cid, p, yb, ya, yas, na, nas in sig:
        s = sp.get(cid)
        if s is None or ya is None or na is None or p is None:
            continue
        R = R_at(ts)
        d = N.inv_cdf(min(max(p, 1e-6), 1 - 1e-6))
        for v, g in VARIANTS.items():
            gam, b = fits[v]
            gr = float(g(np.array([R]))[0])
            q = N.cdf(d * (b / LIVE_B) * math.sqrt(R ** LIVE_GAMMA / gr ** gam))
            smom[v][(s.target_ts, ts // 30 * 30)][s.strike] = (cid, q, [(ya, yas or 0)], [(na, nas or 0)], s.outcome)
            if yb and ya - yb <= 0.10 and 0 < yb <= ya < 1:
                L = srows[v]
                L[0].append(q); L[1].append(s.outcome); L[2].append((yb + ya) / 2); L[3].append(day_of(s.target_ts))
    sdays = sorted(set(srows["current"][3]))
    table("SIGNALS (forward, real time)", {v: (np.array(L[0]), np.array(L[1], float), np.array(L[2]),
                                                np.array(L[3]), np.ones(len(L[0]), bool)) for v, L in srows.items()}, sdays)
    trading("SIGNALS (forward, real time)", smom)


if __name__ == "__main__":
    main()
