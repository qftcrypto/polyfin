"""Value of the Hyperliquid xyz:CL perp for WTI strikes, on the bot's own forward signals.

    .venv/bin/python -m research.perp_value

Each real-time WTI curve (trade.signals: model_p, x_now) is moved by the perp's
return over Yahoo's CL=F delay (LAG, measured live ~10.5 min), as a perp nowcast
would have done; Brier against the book mid and the strike2 rule are compared
with and without the correction.  Needs HL:CL bars (recorder task 'perps').
"""
import math
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
from polyfin.db import connect
from polyfin.stage1 import load_specs
from research.wti_width import simulate, show
ET = ZoneInfo("America/New_York")
LAG = 630                                    # Yahoo CL=F availability delay measured live: ~10.5 min
def perp(t):
    i = bisect_right(pt, t - 60) - 1          # close of the last completed minute
    return pc[i] if i >= 0 and t - pt[i] < 180 else None
conn = connect()
_pb = conn.execute("SELECT ts, close FROM bars WHERE symbol = 'HL:CL' ORDER BY ts").fetchall(); conn.commit()
pt = [r[0] for r in _pb]; pc = [r[1] for r in _pb]
sp = {s.condition_id: s for s in load_specs(conn) if s.series_slug == "wti-daily-close-uo" and s.outcome is not None}
rows = conn.execute("""select s.ts, s.condition_id, s.model_p, s.x_now, s.yes_bid, s.yes_ask, s.yes_ask_size, s.no_ask, s.no_ask_size
   from trade.signals s join markets m using (condition_id) where m.series_slug='wti-daily-close-uo' and m.outcome_yes is not null and s.tau_h > 3""").fetchall()
raw, cor = defaultdict(dict), defaultdict(dict)
by = defaultdict(list)
for ts, cid, p, x, yb, ya, yas, na, nas in rows:
    s = sp.get(cid)
    if s and ya is not None and na is not None and x is not None: by[(s.target_ts, ts // 30 * 30)].append((s.strike, cid, p, x, yb, ya, yas, na, nas, s.outcome))
B = defaultdict(list)                         # (p_raw, p_cor, mid, y)
moved = []
for key, L in by.items():
    L.sort(); ks = [r[0] for r in L]; ps = [r[2] for r in L]
    t = key[1]; a, b = perp(t), perp(t - LAG)
    if a is None or b is None or len(L) < 3: continue
    S = np.median([k * math.exp(x) for k, _, _, x, *_ in L])        # the model's (stale) oil price
    d = S * (a / b - 1); moved.append(d)
    pc_ = list(np.interp(np.array(ks) - d, ks, ps, left=ps[0], right=ps[-1]))   # curve moved by the perp's move
    for (k, cid, p, x, yb, ya, yas, na, nas, y), q in zip(L, pc_):
        raw[key][k] = (cid, p, [(ya, yas or 0)], [(na, nas or 0)], y)
        cor[key][k] = (cid, q, [(ya, yas or 0)], [(na, nas or 0)], y)
        if yb and ya and ya - yb <= 0.10:
            B[datetime.fromtimestamp(key[0], ET).strftime("%m-%d")].append((p, q, (yb + ya) / 2, y))
moved = np.abs(moved)
print(f"WTI forward signals, early slot: {len(by)} moments; perp move over the {LAG/60:.1f}-min Yahoo delay: median ${np.median(moved):.3f}, 90% ${np.percentile(moved, 90):.3f}, max ${moved.max():.2f}")
print(f"\nBrier (real-book points, spread <= 0.10)     {'n':>6} {'live model':>10} {'+ perp':>8} {'market':>8}")
allv = []
for d in sorted(B):
    v = np.array(B[d]); allv.append(v)
    br = lambda i: ((v[:, i] - v[:, 3]) ** 2).mean()
    print(f"   {d}                                {len(v):6d} {br(0):10.4f} {br(1):8.4f} {br(2):8.4f}")
v = np.vstack(allv); br = lambda i: ((v[:, i] - v[:, 3]) ** 2).mean()
print(f"   ALL                                  {len(v):6d} {br(0):10.4f} {br(1):8.4f} {br(2):8.4f}")
print("\nstrike2 rule on the bot's real-time books:")
show("live model (as traded)", simulate(raw, lambda ks, ps, mids: ps))
show("live model + perp correction", simulate(cor, lambda ks, ps, mids: ps))
