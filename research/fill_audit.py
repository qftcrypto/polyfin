"""Our recorded live fills vs the venue's trade records, per order.

    .venv/bin/python -m research.fill_audit      # on the server (needs FIN_ creds)

Matches each live order to the venue trades on its token (by taker order id),
and prints orders whose shares or price differ, plus the fee the venue charged.
"""
from __future__ import annotations

from collections import defaultdict

from polyfin.db import connect
from polyfin.live.clob import ClobExecutor
from polyfin.settings import Credentials


def main() -> None:
    from py_clob_client_v2.clob_types import TradeParams
    conn = connect()
    rows = conn.execute(
        "SELECT id, token_id, created_at, shares_filled, avg_price, fee, status, series_slug, "
        "venue_order_id, reconciled_at IS NOT NULL FROM trade.orders "
        "WHERE mode = 'live' AND shares_filled > 0 ORDER BY id").fetchall()
    conn.commit()
    client = ClobExecutor(Credentials()).client
    venue = defaultdict(list)
    for tok in sorted({r[1] for r in rows}):
        since = min(r[2] for r in rows if r[1] == tok) - 60
        for t in client.get_trades(TradeParams(asset_id=tok)):
            if int(t.get("match_time") or 0) >= since and str(t.get("status", "")).upper() != "FAILED":
                venue[tok].append((float(t["size"]), float(t["price"]), t.get("taker_order_id"),
                                   t.get("fee_rate_bps")))
    bad, d_sh, d_not = [], 0.0, 0.0
    for r in rows:
        oid, tok, _, sh, px, fee, st, slug, voi, rec = r
        vs = [v for v in venue.get(tok, []) if v[2] == voi] if voi else []
        vsh = sum(v[0] for v in vs)
        vpx = sum(v[0] * v[1] for v in vs) / vsh if vsh else 0.0
        if not vs or abs(vsh - sh) > 0.01 or abs(vpx - px) > 0.0005:
            bad.append((oid, slug, st, sh, px, vsh, vpx, rec, voi is not None))
            d_sh += sh - vsh
            d_not += sh * px - vsh * vpx
    fees = defaultdict(int)
    for vs in venue.values():
        for v in vs:
            fees[v[3]] += 1
    print(f"{len(rows)} live orders with fills; venue fee_rate_bps counts: {dict(fees)}; "
          f"fees we booked ${sum(r[5] for r in rows):.2f}")
    print(f"{len(bad)} orders differ from the venue (ours - venue: {d_sh:+.2f} shares, "
          f"${d_not:+.2f} notional)")
    print(f"   {'id':>5} {'market':26} {'status':8} {'ours':>14} {'venue':>14} {'reconciled':>10} oid")
    for oid, slug, st, sh, px, vsh, vpx, rec, has_oid in bad:
        print(f"   {oid:5d} {slug[:26]:26} {st:8} {sh:7.2f}@{px:.3f} {vsh:7.2f}@{vpx:.3f} "
              f"{str(rec):>10} {has_oid}")


if __name__ == "__main__":
    main()
