"""The pre-agreed decision check (agreed 2026-10-03, run once at the 3-week mark).

    .venv/bin/python -m research.decision_check [--since 2026-10-01] [--out FILE]

A paper arm may replace (or be added to) the live rule only if ALL hold, measured
on settled positions from `--since` (settlement date, ET):
  C1  at least 15 trading days of settlements
  C2  bootstrap 90% interval of return per $ (resampling markets) above zero
  C3  still profitable without its best 5 markets
  C4  positive on more than half of its trading days
  C5  no single series supplies more than half of the profit (e.g. not one WTI week)
  C6  return per $ above live's over the same period
Live itself is reported against C1-C5.  No re-slicing: the arms are compared as
they ran.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

import numpy as np

from polyfin.db import connect
from polyfin.varclock import ET

MIN_DAYS, BEST_N, B = 15, 5, 4000


def stats(rows, rng):
    """rows: (condition_id, series, settle_day, cost, pnl)"""
    per, cost, day, ser = defaultdict(float), defaultdict(float), defaultdict(float), defaultdict(float)
    for cid, s, d, c, p in rows:
        per[cid] += p
        cost[cid] += c
        day[d] += p
        ser[s] += p
    keys = list(per)
    pv, cv = np.array([per[k] for k in keys]), np.array([cost[k] for k in keys])
    total, spent = pv.sum(), cv.sum()
    bs = np.array([pv[i].sum() / cv[i].sum()
                   for i in (rng.integers(0, len(pv), len(pv)) for _ in range(B))])
    best = np.sort(pv)[::-1]
    top_series = max(ser.items(), key=lambda kv: kv[1])
    return {
        "markets": len(keys), "cost": spent, "pnl": total, "ret": total / spent if spent else 0.0,
        "lo": float(np.percentile(bs, 5)), "hi": float(np.percentile(bs, 95)),
        "p_loss": float(np.mean(bs < 0)), "wo_best": float(best[BEST_N:].sum()),
        "days": len(day), "days_pos": sum(v > 0 for v in day.values()),
        "top_series": top_series[0], "top_share": top_series[1] / total if total > 0 else float("nan"),
    }


def checks(st, live_ret=None):
    c = {"C1": st["days"] >= MIN_DAYS, "C2": st["lo"] > 0, "C3": st["wo_best"] > 0,
         "C4": st["days_pos"] > st["days"] / 2,
         "C5": st["pnl"] > 0 and st["top_share"] < 0.5}
    if live_ret is not None:
        c["C6"] = st["ret"] > live_ret
    return c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-01")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=ET).timestamp()
    conn = connect()
    rows = conn.execute(
        "SELECT mode, arm, slot, condition_id, series_slug, target_ts, "
        "shares_filled * avg_price + fee, pnl FROM trade.orders "
        "WHERE shares_filled > 0 AND pnl IS NOT NULL AND target_ts >= %s", (since,)).fetchall()
    conn.commit()
    units = defaultdict(list)
    for mode, arm, slot, cid, s, t, c, p in rows:
        d = datetime.fromtimestamp(t, ET).date().isoformat()
        key = "LIVE" if mode == "live" else f"{arm}/{slot}"
        units[key].append((cid, s, d, c, p))
    rng = np.random.default_rng(20261003)
    lines = [f"polyfin decision check - settlements since {args.since}, run "
             f"{datetime.now(ET):%Y-%m-%d %H:%M} ET", ""]
    if "LIVE" not in units:
        lines.append("no settled live positions")
    live = stats(units["LIVE"], rng) if "LIVE" in units else None
    hdr = (f"{'unit':20} {'days':>4} {'mkts':>4} {'cost $':>8} {'pnl $':>8} {'ret/$':>7} "
           f"{'90% interval':>17} {'P(loss)':>7} {'w/o best5':>9} {'days+':>6} "
           f"{'top series (share)':>34}  checks")
    lines.append(hdr)
    order = ["LIVE"] + sorted(k for k in units if k != "LIVE")
    passing = []
    for k in order:
        if k not in units:
            continue
        st = live if k == "LIVE" else stats(units[k], rng)
        ck = checks(st, None if k == "LIVE" else (live["ret"] if live else None))
        ok = all(ck.values())
        if k != "LIVE" and ok:
            passing.append(k)
        lines.append(
            f"{k:20} {st['days']:4d} {st['markets']:4d} {st['cost']:8.2f} {st['pnl']:+8.2f} "
            f"{100 * st['ret']:+6.1f}% {100 * st['lo']:+7.1f}..{100 * st['hi']:+6.1f}% "
            f"{100 * st['p_loss']:6.0f}% {st['wo_best']:+9.2f} {st['days_pos']:3d}/{st['days']:<2d} "
            f"{st['top_series'][:24]:>24} ({100 * st['top_share']:4.0f}%)  "
            + " ".join(f"{n}{'+' if v else '-'}" for n, v in ck.items()))
    lines += ["", "criteria: C1 >= 15 trading days, C2 90% interval > 0, C3 positive without best 5 "
              "markets, C4 positive on > half the days, C5 top series < 50% of profit, "
              "C6 beats live per $",
              "", "VERDICT: " + (f"arms passing every check: {', '.join(passing)}" if passing
                                 else "no paper arm passes every check - keep the live rule")]
    if live:
        lc = checks(live)
        lines.append("live rule itself: " + ("passes C1-C5" if all(lc.values())
                                             else "fails " + ", ".join(n for n, v in lc.items() if not v)))
    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
