"""Stage 2: stage 1 plus three corrections, each with at most one fitted parameter.

1. Futures nowcast (no fitted parameter).  A US stock or cash index is stale
   outside its own trading (SPX has no bars 16:00-09:30; SPY prints thinly
   20:00-04:00), while ES/NQ trade ~23h.  S_hat = S_last * exp(beta * r_fut),
   r_fut = the futures log return since S_last's bar, beta from 15-minute
   regular-hours returns against whichever of ES/NQ fits better.
2. Volatility regime, gamma.  v *= R^gamma, R = realized / expected diffusive
   variance over the trailing 6 hours (clipped to [1/4, 4]).
3. Sharpening, b.  P = Phi(b * d), d the standardized stage 1 score.  Stage 1 is
   underconfident at the top end (0.8-1.0 predictions came true 94-100%).

Optionally a market blend, logit P = w1 logit P_model + w2 logit P_market: in
large model/market disagreements the outcome landed between the two.

    python3 -m polyfin.stage2           # leave-one-day-out evaluation, then fit + save
    python3 -m polyfin.stage2 --live    # price open markets with the saved fit
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import NOWCAST_FUTURES, NOWCAST_SYMBOLS
from .data import load_history
from .settings import ROOT
from .db import connect
from .stage1 import Model, Spec, live, load_specs, norm_cdf
from .varclock import ET, SLOT

PARAMS_PATH = ROOT / "data" / "stage2_params.json"
VOL_WINDOW = 6 * 3600
R_CLIP = (0.25, 4.0)
MIN_R2 = 0.1                      # below this the futures say little about the stock

GAMMAS = np.linspace(0.0, 1.0, 5)
SHARPS = np.linspace(0.6, 2.5, 39)
SHARPEN_HOURS = 3.0               # live pricing sharpens only this close to the target


class Stage2:
    def __init__(self, conn, params: dict | None = None, betas: dict | None = None):
        self.m1 = Model(conn)
        self.params = params or {"gamma": 0.0, "b": 1.0}
        # pass a shared dict to reuse betas across rebuilds (they move slowly)
        self._beta: dict = betas if betas is not None else {}

    # -- 1. futures nowcast ---------------------------------------------------
    def beta(self, sym: str):
        """(futures symbol, beta, r2) for a nowcast symbol, or None."""
        if sym in self._beta:
            return self._beta[sym]
        best = None
        if sym in NOWCAST_SYMBOLS:
            bars = self.m1._get(sym)[0]
            for fut in NOWCAST_FUTURES:
                fb = self.m1._get(fut)[0]
                xs, ys = [], []
                for t in range(int(bars.ts[0]) // SLOT * SLOT, int(bars.ts[-1]), SLOT):
                    lt = bars.last_bar(t + SLOT)
                    if not lt or lt[0] < t:              # no print in the slot
                        continue
                    a, z = bars.price_at(t), lt[1]
                    fa, fz = fb.price_at(t), fb.price_at(lt[0] + 60)
                    if a and fa and fz and _rth(t):
                        xs.append(math.log(fz / fa))
                        ys.append(math.log(z / a))
                if len(xs) > 50:
                    x, y = np.array(xs), np.array(ys)
                    b = float(x @ y / (x @ x))
                    r2 = float((x @ y) ** 2 / ((x @ x) * (y @ y)))
                    if r2 >= MIN_R2 and (best is None or r2 > best[2]):
                        best = (fut, b, r2)
        self._beta[sym] = best
        return best

    def nowcast(self, sym: str, t: float) -> float | None:
        bars = self.m1._get(sym)[0]
        lb = bars.last_bar(t)
        if lb is None:
            return None
        ts, px = lb
        fb = self.beta(sym)
        if fb is None or t - (ts + 60) < 120:          # fresh print: nothing to add
            return px
        fut, beta, _ = fb
        f0, f1 = self.m1._get(fut)[0].price_at(ts + 60), self.m1._get(fut)[0].price_at(t)
        if not f0 or not f1:
            return px
        return px * math.exp(beta * math.log(f1 / f0))

    # -- features / probability -----------------------------------------------
    def features(self, s: Spec, t: float, exclude_day=None):
        """(x_raw, x_nowcast, v, R) at t, or None."""
        if t >= s.target_ts:
            return None
        bars, clock = self.m1._get(s.symbol)
        raw = bars.price_at(t)
        if raw is None:
            return None
        rv, ev = clock.realized(t - VOL_WINDOW, t, exclude_day)
        R = min(max(rv / ev, R_CLIP[0]), R_CLIP[1]) if ev > 0 and rv > 0 else 1.0
        if s.kind != "strikes" and s.ref_ts > t:
            return 0.0, 0.0, clock.var(s.ref_ts, s.target_ts, exclude_day), R
        ref = self.m1.reference(s)
        now = self.nowcast(s.symbol, t)
        if not ref or not now:
            return None
        return (math.log(raw / ref), math.log(now / ref),
                clock.var(t, s.target_ts, exclude_day), R)

    def prob(self, s: Spec, t: float, exclude_day=None) -> float | None:
        f = self.features(s, t, exclude_day)
        if f is None:
            return None
        # sharpening helps near the target and hurts further out (see evaluate())
        tau_h = (s.target_ts - t) / 3600
        b = self.params["b"] if tau_h <= self.params.get("sharpen_hours", 99) else 1.0
        return float(prob_vec(np.array([f[1]]), np.array([f[2]]), np.array([f[3]]),
                              self.params["gamma"], b)[0])


def _rth(t: float) -> bool:
    d = datetime.fromtimestamp(t, ET)
    return d.weekday() < 5 and (9, 30) <= (d.hour, d.minute) < (16, 0)


def prob_vec(x, v, R, gamma, b):
    v2 = v * R ** gamma
    with np.errstate(divide="ignore", invalid="ignore"):
        d = np.where(v2 > 1e-14, (x - v2 / 2) / np.sqrt(np.maximum(v2, 1e-300)),
                     np.sign(x) * np.inf)
    return np.vectorize(norm_cdf)(b * d)


# -- evaluation -------------------------------------------------------------------
def logit(p):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


def logloss(p, y):
    p = np.clip(p, 1e-3, 1 - 1e-3)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def collect(model: Stage2, specs, conn, step=15, hours=36, min_volume=500):
    volume = dict(conn.execute(
        "SELECT condition_id, COALESCE((raw->>'volume')::float, 0) FROM markets"))
    rows = []
    for s in specs:
        if s.outcome is None or float(volume.get(s.condition_id) or 0) < min_volume:
            continue
        hist, ex = load_history(conn, s.token_yes), model.m1.exclude_for(s)
        t = s.target_ts - hours * 3600
        while t < s.target_ts:
            f, m = model.features(s, t, ex), hist.at(t)
            if f is not None and m is not None:
                rows.append((*f, m, s.outcome, (s.target_ts - t) / 3600, str(ex), s.kind,
                             s.condition_id))
            t += step * 60
    cols = list(zip(*rows))
    arr = {k: np.array(cols[i], dtype=float) for i, k in
           enumerate(["xr", "xn", "v", "R", "pm", "y", "tau"])}
    arr["day"] = np.array(cols[7])
    arr["kind"] = np.array(cols[8])
    arr["cid"] = np.array(cols[9])
    return arr


def fit_sharp(a, idx, use_nowcast=True, fit_gamma=True, fit_b=True):
    """Grid-search (gamma, b) minimizing log loss on rows idx."""
    x = a["xn" if use_nowcast else "xr"][idx]
    best = (np.inf, 0.0, 1.0)
    for g in (GAMMAS if fit_gamma else [0.0]):
        for b in (SHARPS if fit_b else [1.0]):
            ll = logloss(prob_vec(x, a["v"][idx], a["R"][idx], g, b), a["y"][idx]).mean()
            if ll < best[0]:
                best = (ll, float(g), float(b))
    return {"gamma": best[1], "b": best[2]}


def fit_blend(pmodel, pmkt, y):
    """Logistic regression without intercept on (logit p_model, logit p_market)."""
    X = np.column_stack([logit(pmodel), logit(pmkt)])
    w = np.array([0.5, 0.5])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y)
        H = X.T @ (X * (p * (1 - p))[:, None]) + 1e-6 * np.eye(2)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def evaluate(a):
    """Leave-one-day-out predictions for each variant."""
    days = sorted(set(a["day"]))
    n = len(a["y"])
    out = {k: np.full(n, np.nan) for k in
           ["stage1", "+nowcast", "+vol", "+sharpen", "sharp<=3h", "+blend", "market"]}
    out["stage1"] = prob_vec(a["xr"], a["v"], a["R"], 0.0, 1.0)
    out["+nowcast"] = prob_vec(a["xn"], a["v"], a["R"], 0.0, 1.0)
    out["market"] = a["pm"]
    fits = []
    for d in days:
        test, train = a["day"] == d, a["day"] != d
        pv = fit_sharp(a, train, fit_b=False)
        out["+vol"][test] = prob_vec(a["xn"][test], a["v"][test], a["R"][test], pv["gamma"], 1.0)
        pf = fit_sharp(a, train)
        full = lambda m: prob_vec(a["xn"][m], a["v"][m], a["R"][m], pf["gamma"], pf["b"])
        out["+sharpen"][test] = full(test)
        near = test & (a["tau"] <= SHARPEN_HOURS)
        out["sharp<=3h"][test] = prob_vec(a["xn"][test], a["v"][test], a["R"][test], pf["gamma"], 1.0)
        out["sharp<=3h"][near] = full(near)
        w = fit_blend(full(train), a["pm"][train], a["y"][train])
        out["+blend"][test] = 1 / (1 + np.exp(-(w[0] * logit(full(test)) + w[1] * logit(a["pm"][test]))))
        fits.append((d, pv["gamma"], pf["gamma"], pf["b"], w))
    return out, fits


def report(a, out, fits):
    print("== leave-one-day-out fits (held-out day: vol-only gamma | full gamma, b | blend w_model, w_mkt)")
    for d, g1, g, b, w in fits:
        print(f"   {d}  {g1:.2f} | {g:.2f}, {b:.2f} | {w[0]:.2f}, {w[1]:.2f}")
    groups = [("all", np.ones_like(a["y"], dtype=bool))]
    groups += [(k, a["kind"] == k) for k in ("updown", "open", "strikes")]
    groups += [(f"{lo}-{hi}h", (a["tau"] >= lo) & (a["tau"] < hi))
               for lo, hi in [(0, 1), (1, 3), (3, 7), (7, 24), (24, 99)]]
    for metric, fn in [("brier", lambda p, y: (p - y) ** 2), ("log loss", logloss)]:
        print(f"\n== {metric}, held-out (lower is better)")
        print(f"   {'':9} {'n':>6} {'mkts':>5}" + "".join(f"{k:>10}" for k in out))
        for name, m in groups:
            if m.sum():
                print(f"   {name:9} {m.sum():6d} {len(set(a['cid'][m])):5d}" +
                      "".join(f"{fn(p[m], a['y'][m]).mean():10.4f}" for p in out.values()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None, help="libpq DSN; default from FIN_PG* in .env")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    conn = connect(args.dsn)
    if args.live:
        params = json.loads(PARAMS_PATH.read_text())
        print(f"stage 2 params: {params}\n")
        live(conn, Stage2(conn, params))
        return
    model = Stage2(conn)
    specs = load_specs(conn)
    t0 = time.time()
    a = collect(model, specs, conn)
    print(f"{len(a['y'])} points, {len(set(a['cid']))} markets, "
          f"{len(set(a['day']))} days ({time.time() - t0:.0f}s)\n")
    print("== futures betas (15m regular-hours returns)")
    for sym in sorted({s.symbol for s in specs if s.symbol in NOWCAST_SYMBOLS}):
        fb = model.beta(sym)
        print(f"   {sym:6} " + (f"{fb[0]:5} beta={fb[1]:5.2f} r2={fb[2]:.2f}" if fb else "none"))
    out, fits = evaluate(a)
    report(a, out, fits)

    params = fit_sharp(a, a["tau"] <= SHARPEN_HOURS)
    params["gamma"] = fit_sharp(a, np.ones_like(a["y"], dtype=bool), fit_b=False)["gamma"]
    params["sharpen_hours"] = SHARPEN_HOURS
    pfull = prob_vec(a["xn"], a["v"], a["R"], params["gamma"], params["b"])
    params["blend"] = [float(x) for x in fit_blend(pfull, a["pm"], a["y"])]
    params["fitted_at"] = int(time.time())
    PARAMS_PATH.write_text(json.dumps(params, indent=1))
    print(f"\nfit on all days: {params} -> {PARAMS_PATH}")


if __name__ == "__main__":
    main()
