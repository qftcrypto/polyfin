"""WTI strikes: old model vs new vs 'old, widen-only' on the bot's forward signals.

    .venv/bin/python -m research.strike_upwide

old        gamma 0.5 on R (6h realized / normal, 0.25..4), strike_b 1.3 - live to 10/16,
           frozen in strike2_regime (logged as model_p_regime from 10/18)
new        no regime scaling, strike_b 1.15 (WTI) - live from 10/18 (model_p)
widen-only the old model with R floored at 1: turbulence widens, a quiet past never narrows
Probabilities are rescaled from the logged ones: d = Phi^-1(p); the variance ratio between
two models is (R^g1 / R'^g2), so d2 = d1 * (b2/b1) * sqrt(var1/var2).  R is recomputed at
each moment from CL=F bars.  Scored on Brier vs the book mid (spread <= 0.10, > 3h out),
closeness in >= 0.08 disagreements, and strike2 (0.08, 2 buys, no entries 18:00-06:00 ET).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from statistics import NormalDist
from zoneinfo import ZoneInfo

import numpy as np

from polyfin.db import connect
from polyfin.stage1 import load_specs
from polyfin.stage2 import R_CLIP, VOL_WINDOW, Stage2

from .strike_threshold import simulate

ET = ZoneInfo("America/New_York")
N = NormalDist()
SERIES = "wti-daily-close-uo"
NEW_LIVE = 1792195200          # 2026-10-18 00:00 ET: model_p is the new model from here


def main() -> None:
    conn = connect()
    sp = {s.condition_id: s for s in load_specs(conn) if s.series_slug == SERIES and s.outcome is not None}
    clock = Stage2(conn).m1._get("CL=F")[1]
    rows = conn.execute(
        "SELECT s.ts, s.condition_id, s.model_p, s.model_p_regime, s.yes_bid, s.yes_ask, s.yes_ask_size, "
        "s.no_ask, s.no_ask_size FROM trade.signals s JOIN markets m USING (condition_id) "
        "WHERE m.series_slug = %s AND m.outcome_yes IS NOT NULL AND s.tau_h > 3", (SERIES,)).fetchall()
    conn.commit()
    Rc = {}

    def R_at(ts):
        k = ts // 300
        if k not in Rc:
            rv, ev = clock.realized(ts - VOL_WINDOW, ts)
            Rc[k] = min(max(rv / ev, R_CLIP[0]), R_CLIP[1]) if ev > 0 and rv > 0 else 1.0
        return Rc[k]
    V = ("old", "new", "widen-only")
    score = {v: defaultdict(list) for v in V}
    mom = {v: defaultdict(dict) for v in V}
    for ts, cid, p, preg, yb, ya, yas, na, nas in rows:
        s = sp.get(cid)
        if s is None or ya is None or na is None or p is None:
            continue
        R = R_at(ts)
        if ts >= NEW_LIVE:
            if preg is None:
                continue
            d_old = N.inv_cdf(min(max(preg, 1e-6), 1 - 1e-6))
            q = {"old": preg, "new": p}
        else:                                           # model_p was the old model
            d_old = N.inv_cdf(min(max(p, 1e-6), 1 - 1e-6))
            q = {"old": p, "new": N.cdf(d_old * (1.15 / 1.3) * R ** 0.25)}
        q["widen-only"] = N.cdf(d_old * min(R, 1.0) ** 0.25)
        day = datetime.fromtimestamp(s.target_ts, ET).strftime("%m-%d")
        for v in V:
            if 6 <= datetime.fromtimestamp(ts, ET).hour < 18:   # live's entry hours
                mom[v][(s.target_ts, ts // 30 * 30)][s.strike] = (cid, q[v], [(ya, yas or 0)], [(na, nas or 0)], s.outcome)
            if yb and 0 < yb <= ya < 1 and ya - yb <= 0.10:
                score[v][day].append((q[v], s.outcome, (yb + ya) / 2))
    days = sorted(score["old"])
    print("Brier where we trade (lower is better), and % closer than the market when they disagree by >= 0.08")
    print(f"   {'day':6} {'n':>6} " + "".join(f"{v:>22}" for v in V) + f"{'market':>9}")
    tot = {v: [] for v in V}
    for d in days:
        cells = ""
        for v in V:
            a = np.array(score[v][d]); tot[v].append(a)
            dis = np.abs(a[:, 0] - a[:, 2]) >= 0.08
            closer = np.mean(np.abs(a[dis, 0] - a[dis, 1]) < np.abs(a[dis, 2] - a[dis, 1])) if dis.any() else float("nan")
            cells += f"{((a[:, 0] - a[:, 1]) ** 2).mean():10.4f} ({100 * closer:3.0f}% {dis.sum():5d})"
        a = np.array(score["old"][d])
        print(f"   {d:6} {len(a):6d} {cells}{((a[:, 2] - a[:, 1]) ** 2).mean():9.4f}")
    cells = ""
    for v in V:
        a = np.vstack(tot[v]); dis = np.abs(a[:, 0] - a[:, 2]) >= 0.08
        cells += f"{((a[:, 0] - a[:, 1]) ** 2).mean():10.4f} ({100 * np.mean(np.abs(a[dis, 0] - a[dis, 1]) < np.abs(a[dis, 2] - a[dis, 1])):3.0f}% {dis.sum():5d})"
    a = np.vstack(tot["old"])
    print(f"   {'ALL':6} {len(a):6d} {cells}{((a[:, 2] - a[:, 1]) ** 2).mean():9.4f}")
    print("\nstrike2 at edge 0.08, entries 06:00-18:00 ET (top of book)")
    for v in V:
        F = simulate(mom[v], 0.08, 2, 10.0)
        byd = defaultdict(float)
        for cid, tg, c, pl in F:
            byd[tg] += pl
        C, P = sum(f[2] for f in F), sum(f[3] for f in F)
        print(f"   {v:11} {len(F):4d} buys  pnl {P:+8.2f} ({100 * P / C if C else 0:+6.1f}%)  days+ "
              f"{sum(x > 0 for x in byd.values())}/{len(byd)}  " +
              " ".join(f"{datetime.fromtimestamp(d, ET):%m-%d}:{x:+.0f}" for d, x in sorted(byd.items())))


if __name__ == "__main__":
    main()
