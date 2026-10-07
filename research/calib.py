"""Calibration layer on top of stage 2: logit P' = a + b * logit P, fitted per group.

    .venv/bin/python -m research.calib

Fitted leave-one-day-out on resolved outcomes only (no market price is used to fit
or to predict).  Scored by Brier on points with a recorded book (spread <= 0.10)
against the book mid, per market type and time window.
"""
from __future__ import annotations

from bisect import bisect_right

import numpy as np

from polyfin.db import connect
from polyfin.stage1 import load_specs
from polyfin.stage2 import Stage2, collect, evaluate, logit

from .zones import book_index


def fit_platt(z, y, intercept=True, l2=1.0):
    """Logistic regression of y on z (=logit p), ridge toward a=0, b=1."""
    X = np.column_stack([np.ones_like(z), z]) if intercept else z[:, None]
    prior = np.array([0.0, 1.0]) if intercept else np.array([1.0])
    w = prior.copy()
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ w))
        g = X.T @ (p - y) + l2 * (w - prior)
        H = X.T @ (X * (p * (1 - p))[:, None]) + l2 * np.eye(len(w))
        w -= np.linalg.solve(H, g)
    return w


def apply(w, z):
    return 1 / (1 + np.exp(-((w[0] + w[1] * z) if len(w) == 2 else w[0] * z)))


def groups(kind, tau, scheme):
    k = np.where(kind == "strikes", "strikes", np.where(kind == "open", "open", "updown"))
    if scheme == "global":
        return np.full(len(kind), "all")
    if scheme == "kind":
        return k
    t = np.where(tau <= 3, "<=3h", np.where(tau <= 12, "3-12h", ">12h"))
    return np.char.add(np.char.add(k.astype(str), "|"), t.astype(str))


def lodo(p, a, scheme, intercept):
    z, y, day = logit(p), a["y"], a["day"]
    g = groups(a["kind"], a["tau"], scheme)
    out = np.full(len(y), np.nan)
    for d in sorted(set(day)):
        tr, te = day != d, day == d
        for G in set(g[te]):
            m_tr, m_te = tr & (g == G), te & (g == G)
            if m_tr.sum() < 200:
                out[m_te] = p[m_te]
                continue
            out[m_te] = apply(fit_platt(z[m_tr], y[m_tr], intercept), z[m_te])
    return out


def main() -> None:
    conn = connect()
    specs = load_specs(conn)
    a = collect(Stage2(conn), specs, conn, step=5, hours=36)
    out, _ = evaluate(a)
    p = out["sharp<=3h"]
    sp = {s.condition_id: s for s in specs}
    bi = book_index(conn)
    y, tau = a["y"], a["tau"]
    bm = np.full(len(y), np.nan)
    for i in range(len(y)):
        s = sp[a["cid"][i]]
        t = s.target_ts - tau[i] * 3600
        bt, bv = bi.get(s.token_yes, ([], []))
        j = bisect_right(bt, t) - 1
        if j >= 0 and t - bt[j] <= 120:
            b, k = bv[j]
            if b is not None and k is not None and 0 < b <= k < 1 and k - b <= 0.10:
                bm[i] = (b + k) / 2
    ok = ~np.isnan(bm)
    variants = {"raw model": p}
    for scheme in ("global", "kind", "kind x time"):
        for ic in (False, True):
            variants[f"{scheme}, slope{' + intercept' if ic else ''}"] = \
                lodo(p, a, "kt" if scheme == "kind x time" else scheme, ic)
    B = lambda q, g: ((q[g] - y[g]) ** 2).mean()
    k = np.where(a["kind"] == "strikes", "strikes", np.where(a["kind"] == "open", "open", "updown"))
    cells = [("all", ok), ("early >3h", ok & (tau > 3)), ("late <=3h", ok & (tau <= 3)),
             ("updown", ok & (k == "updown")), ("strikes", ok & (k == "strikes")),
             ("open", ok & (k == "open"))]
    print(f"{ok.sum()} real-book points, {len(set(a['day'][ok]))} days; Brier (lower is better)\n")
    print(f"   {'variant':30}" + "".join(f"{c:>11}" for c, _ in cells))
    print(f"   {'MARKET (book mid)':30}" + "".join(f"{B(bm, g):11.4f}" for _, g in cells))
    for name, q in variants.items():
        print(f"   {name:30}" + "".join(f"{B(q, g):11.4f}" for _, g in cells))
    best = min((v for v in variants if v != "raw model"), key=lambda v: B(variants[v], ok & (tau > 3)))
    q = variants[best]
    d = np.abs(q - bm) >= 0.10
    g = ok & d
    print(f"\nbest early variant: {best}")
    print(f"   disagreements >= 0.10: {g.sum()} points, model closer {100 * np.mean(np.abs(q[g] - y[g]) < np.abs(bm[g] - y[g])):.0f}% "
          f"(raw model: {100 * np.mean(np.abs(p[ok & (np.abs(p - bm) >= 0.10)] - y[ok & (np.abs(p - bm) >= 0.10)]) < np.abs(bm[ok & (np.abs(p - bm) >= 0.10)] - y[ok & (np.abs(p - bm) >= 0.10)])):.0f}%)")
    print("   per-day early gap (model - market):", " ".join(
        f"{d[5:]}:{B(q, ok & (tau > 3) & (a['day'] == d)) - B(bm, ok & (tau > 3) & (a['day'] == d)):+.4f}"
        for d in sorted(set(a['day'][ok]))))
    print("   fit on all days:", {G: np.round(fit_platt(logit(p)[groups(a['kind'], tau, 'kt') == G],
          y[groups(a['kind'], tau, 'kt') == G], True), 2).tolist() for G in sorted(set(groups(a['kind'], tau, 'kt')))})


if __name__ == "__main__":
    main()
