#!/usr/bin/env python3
"""Nightly arm report, on the laptop (launchd: deploy/com.polyfin.arm-report.plist).

The polycrypto zone report's template (polycrypto/scripts/zone_report.py) for
polyfin: every arm in polyfin/live/config.py ARMS - live first, then paper; arms no
longer configured are not reported (operator 2026-10-08) - with its FORWARD record (actual live and
paper fills, settled) over the last 1, 2, 3, 7 and 14 trading days with
settlements and all history.  -> data/reports/arm_report.html (+ a dated copy),
the local `arm_report` table, and a desktop notification.

    1. prod, ONE ssh, read-only: every filled order (live and paper)
    2. summarise per arm x market type x period, store, render, notify

    .venv/bin/python scripts/arm_report.py
    .venv/bin/python scripts/arm_report.py --no-sync      # reuse the last sync
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from polyfin.db import connect  # noqa: E402
from polyfin.live import config as C  # noqa: E402

ET = ZoneInfo("America/New_York")
SSH = [os.path.expanduser(t) for t in shlex.split(os.environ.get(
    "POLYFIN_SOURCE_SSH", "ssh -i ~/.ssh/id_ed25519_qftcrypto_ub -p 56777 -o ConnectTimeout=20 "
    "-o ServerAliveInterval=30 -o BatchMode=yes deploy@46.246.92.205"))]
OUT = os.path.join(ROOT, "data", "reports")
SYNC = os.path.join(OUT, "prod_sync.json")
PERIODS = (("1d", 1), ("2d", 2), ("3d", 3), ("7d", 7), ("14d", 14), ("all", None))
KINDS = (("strikes", "strikes"), ("updown", "up/down"), ("open", "opens"))

#: Runs ON PROD (psql in the database container), read-only. One JSON array.
REMOTE = ("docker exec polyfin-timescaledb psql -U polyfin -d polyfin -At -c "
          + shlex.quote(
              "select coalesce(json_agg(t), '[]') from (select mode, arm, kind, slot, series_slug, "
              "target_ts, created_at, shares_filled, avg_price, fee, pnl from trade.orders "
              "where shares_filled > 0) t"))


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def sync_prod():
    """One ssh call, never retried (fail2ban): a failure stops the run."""
    r = subprocess.run(SSH + [REMOTE], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError("prod read failed (%d): %s" % (r.returncode, r.stderr.strip()[-400:]))
    rows = json.loads(r.stdout)
    os.makedirs(OUT, exist_ok=True)
    with open(SYNC, "w") as f:
        json.dump({"orders": rows, "synced_at": int(time.time())}, f)
    log("prod synced: %d filled orders" % len(rows))
    return {"orders": rows, "synced_at": int(time.time())}


def notify(title, text):
    subprocess.run(["osascript", "-e", "display notification %s with title %s" % (
        json.dumps(text), json.dumps(title))], capture_output=True)


def et_day(ts):
    return dt.datetime.fromtimestamp(ts, ET).date()


def describe(cfg) -> str:
    """The arm's rule in one line, read from the arm as configured."""
    g = lambda x: ("%.4f" % x).rstrip("0").rstrip(".")
    parts = ["edge >= %s%s" % (g(cfg["min_edge"]), " < %s" % g(cfg["max_edge"]) if "max_edge" in cfg else "")]
    if "rungs" in cfg and len(cfg["rungs"]) > 1:
        parts.append("buys at edge " + " / ".join(g(r) for r in cfg["rungs"]))
    if "repeat" in cfg:
        rp = cfg["repeat"]
        parts.append("repeat every %d min, <= %d buys, <= $%s/mkt%s" % (
            rp["spacing_s"] // 60, rp["max_buys"], g(rp["market_cap_usd"]),
            "" if cfg.get("flip") is None else ", flip at %s" % g(cfg["flip"])))
    parts.append("slot " + "+".join(sorted(cfg["slots"])))
    if "kinds" in cfg:
        parts.append("only " + "+".join(dict(KINDS).get(k, k) for k in sorted(cfg["kinds"])))
    if "min_price" in cfg:
        parts.append("ask >= %s" % g(cfg["min_price"]))
    if "max_tau_h" in cfg:
        parts.append("last %sh" % g(cfg["max_tau_h"]))
    if "confirm_s" in cfg:
        parts.append("edge held %ds" % cfg["confirm_s"])
    if "blend" in cfg:
        parts.append("model+market blend %s" % "/".join(g(w) for w in cfg["blend"]))
    parts.append("$%s/buy, <= ask + %dc" % (g(C.BUDGET_USD), round(100 * C.MAX_SLIP)))
    return ", ".join(parts)


def summarise(F):
    """Fills, wins, per-share edge after fee in pp (+- s.e.), P&L and cost, settled fills only."""
    S = [f for f in F if f["pnl"] is not None]
    if not S:
        return dict(n=0, won=0, edge=None, se=None, pnl=0.0, cost=0.0)
    e = [100 * f["pnl"] / f["shares_filled"] for f in S]          # = outcome - price - fee per share
    m = sum(e) / len(e)
    se = math.sqrt(sum((x - m) ** 2 for x in e) / (len(e) - 1) / len(e)) if len(e) > 1 else None
    return dict(n=len(S), won=sum(f["pnl"] > 0 for f in S), edge=m, se=se,
                pnl=sum(f["pnl"] for f in S),
                cost=sum(f["shares_filled"] * f["avg_price"] + (f["fee"] or 0) for f in S))


def run(orders, now):
    """Rows: (section, arm, kind, period) summaries with the arm's status and rule."""
    for o in orders:
        o["day"] = et_day(o["target_ts"])
    settled_days = sorted({o["day"] for o in orders if o["pnl"] is not None})
    units = defaultdict(list)                   # (mode, arm) -> fills
    for o in orders:
        units[(o["mode"], o["arm"])].append(o)
    out = []
    for (mode, arm), F in units.items():
        cfg = C.ARMS[mode].get(arm)
        if cfg is None:                         # retired arm: not tracked
            continue
        section, status = ("live", "LIVE") if mode == "live" else ("paper", "paper")
        desc = describe(cfg)
        started = min(o["created_at"] for o in F)
        twin = units.get(("paper", arm)) if mode == "live" else None
        for name, n in PERIODS:
            days = settled_days if n is None else settled_days[-n:]
            if not days:
                continue
            # the template's rule: a window the arm did not run through is N/A, never a
            # "-" or $0 that reads as data.  "Ran through" = trading before its first day.
            na = n is not None and (len(settled_days) < n or
                                    started > dt.datetime.combine(days[0], dt.time(), ET).timestamp())
            sel = [o for o in F if o["day"] in set(days)]
            base = dict(section=section, mode=mode, arm=arm, status=status, desc=desc, period=name,
                        na=na, days=(days[0], days[-1]), started=started)
            out.append(dict(base, kind="all", **summarise(sel)))
            for k, _ in KINDS:
                out.append(dict(base, kind=k, **summarise([o for o in sel if o["kind"] == k])))
            if twin is not None:
                out.append(dict(base, kind="paper twin", **summarise([o for o in twin if o["day"] in set(days)])))
    return out, settled_days


DDL = """create table if not exists arm_report (
    run_day date not null, mode text not null, arm text not null, status text,
    kind text not null, period text not null, na boolean, n int, won int,
    edge_pp double precision, se_pp double precision, pnl double precision, cost double precision,
    created_at timestamptz default now(),
    primary key (run_day, mode, arm, kind, period))"""


def store(conn, rows, run_day):
    conn.execute(DDL)
    conn.execute("delete from arm_report where run_day = %s", (run_day,))
    conn.executemany(
        "insert into arm_report (run_day, mode, arm, status, kind, period, na, n, won, edge_pp, se_pp, pnl, cost) "
        "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        [(run_day, r["mode"], r["arm"], r["status"], r["kind"], r["period"], r["na"], r["n"], r["won"],
          r["edge"], r["se"], r["pnl"], r["cost"]) for r in rows])
    conn.commit()


def flags(rows):
    """(mode, arm) -> verdict text and class, from the 7d and 14d forward record."""
    by = defaultdict(dict)
    for r in rows:
        by[(r["mode"], r["arm"])][(r["kind"], r["period"])] = r
    out = {}
    for key, d in by.items():
        dec = [d.get(("all", p)) for p in ("7d", "14d")]
        live = key[0] == "live" and d[("all", "all")]["status"] == "LIVE"
        if any(x is None or x["na"] for x in dec):
            seven = d.get(("all", "7d"))
            if live and seven and not seven["na"] and seven["n"] and seven["pnl"] < 0:
                out[key] = ("live, losing over 7d", "bad")
            else:
                out[key] = ("N/A - not 7 and 14 trading days of record yet", "")
            continue
        all_pos = all(x["n"] and x["pnl"] > 0 for x in dec)
        neg7 = dec[0]["n"] and dec[0]["pnl"] < 0
        if live and neg7:
            out[key] = ("live, losing over 7d", "bad")
        elif all_pos and live:
            out[key] = ("live, positive in 7d and 14d", "good")
        elif all_pos:
            out[key] = ("positive in 7d and 14d - not live", "good")
        elif not any(x["n"] for x in dec):
            out[key] = ("no trades", "")
        else:
            out[key] = ("not positive in 7d and 14d", "")
    return out


def _roi(r):
    return "" if not r.get("cost") else " &middot; ROI %+.0f%%" % (100 * r["pnl"] / r["cost"])


def _cell(r):
    if r and r.get("na"):
        return '<span class="muted">N/A</span>'
    if not r or not r["n"]:
        return '<span class="muted">-</span>'
    cls = "pos" if r["pnl"] > 0 else "neg" if r["pnl"] < 0 else ""
    se = "" if r["se"] is None else " &plusmn;%.1f" % r["se"]
    return '<span class="%s">%+.1fpp%s</span> <span class="muted">&middot; %d &middot; $%+.2f%s</span>' % (
        cls, r["edge"], se, r["n"], r["pnl"], _roi(r))


def _days(r, period):
    if not r:
        return ""
    a, b = r["days"]
    f = lambda d: d.strftime("%m-%d")
    lab = f(a) if a == b else "%s - %s" % (f(a), f(b))
    if period == "all":
        lab += ", since %s" % dt.datetime.fromtimestamp(r["started"], ET).strftime("%m-%d %H:%M")
    return '<div class="sub">%s</div>' % lab


SECTIONS = (("live", "Live (real money)"), ("paper", "Paper arms"))


def render(rows, path, meta: dict):
    by = defaultdict(dict)
    for r in rows:
        by[(r["mode"], r["arm"])][(r["kind"], r["period"])] = r
    fl = flags(rows)
    parts = []
    for sec, title in SECTIONS:
        keys = sorted(k for k in by if by[k][("all", "all")]["section"] == sec)
        if not keys:
            continue
        trs = []
        for key in keys:
            d = by[key]
            head = d[("all", "all")]
            v, vc = fl[key]
            subs = [(k, lab) for k, lab in KINDS if any(d.get((k, p)) and d[(k, p)]["n"] for p, _ in PERIODS)]
            if any(d.get(("paper twin", p)) for p, _ in PERIODS):
                subs.append(("paper twin", "paper twin"))
            cells = "".join(
                "<td>%s%s%s</td>" % (
                    _cell(d.get(("all", p))), _days(d.get(("all", p)), p),
                    "".join('<div class="sub">%s %s</div>' % (lab, _cell(d.get((k, p)))) for k, lab in subs))
                for p, _ in PERIODS)
            trs.append('<tr><th scope="row">%s<div class="sub">%s</div><div class="sub rule">%s</div></th>'
                       '%s<td class="%s">%s</td></tr>' % (
                           html.escape(key[1]), html.escape(head["status"]), html.escape(head["desc"]),
                           cells, vc, html.escape(v)))
        parts.append('<section><h2>%s</h2><div class="wrap"><table><thead><tr><th>Arm</th>%s<th>Verdict</th>'
                     '</tr></thead><tbody>%s</tbody></table></div></section>' % (
                         html.escape(title),
                         "".join("<th>%s</th>" % ("All history" if n is None else
                                                   "Last %d trading day%s" % (n, "" if n == 1 else "s"))
                                 for _, n in PERIODS), "".join(trs)))
    page = TEMPLATE.replace("{{BODY}}", "".join(parts)).replace("{{META}}", html.escape(meta.get("line", ""))) \
        .replace("{{NOTES}}", "".join("<li>%s</li>" % html.escape(n) for n in meta.get("notes", [])))
    with open(path, "w") as f:
        f.write(page)


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Polyfin Arm Report</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1d1b;--muted:#77756f;--line:#e4e1da;--pos:#1f7a3f;--neg:#b3261e;--card:#fff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#171716;--fg:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--pos:#5fc28a;--neg:#f08a80;--card:#1f1f1d}}
:root[data-theme="dark"]{--bg:#171716;--fg:#ecebe8;--muted:#9a978f;--line:#2e2d2a;--pos:#5fc28a;--neg:#f08a80;--card:#1f1f1d}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 48px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 8px}.meta{color:var(--muted);margin:0 0 16px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:1180px}th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}
thead th{font-weight:600;color:var(--muted);font-size:12px}tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
.sub{color:var(--muted);font-size:12px;margin-top:2px}.rule{font-weight:400;max-width:190px;line-height:1.35}.muted{color:var(--muted)}.pos{color:var(--pos)}.neg{color:var(--neg)}
td.good{color:var(--pos);font-weight:600}td.bad{color:var(--neg);font-weight:600}ul{color:var(--muted);padding-left:18px}
</style></head><body><main><h1>Polyfin arm report</h1><p class="meta">{{META}}</p>{{BODY}}
<h2>How to read it</h2><ul>{{NOTES}}</ul></main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    lock = open(os.path.join(OUT, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another run holds the lock; exiting")
        return 0
    t0 = time.time()
    try:
        data = json.load(open(SYNC)) if a.no_sync else sync_prod()
        orders = data["orders"]
        rows, days = run(orders, time.time())
        day = dt.datetime.now(ET).date()
        conn = connect()
        store(conn, rows, day)
        fl = flags(rows)
        bad = sorted("%s %s" % k for k, (v, c) in fl.items() if c == "bad")
        cand = sorted("%s %s" % k for k, (v, c) in fl.items() if v.startswith("positive in 7d and 14d - not"))
        opn = [o for o in orders if o["pnl"] is None]
        lv = [o for o in opn if o["mode"] == "live"]
        line = ("run %s ET; prod synced %s; %d trading days settled (%s - %s); open now: live %d positions "
                "($%.2f), paper %d; runtime %.0fs" % (
                    dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
                    dt.datetime.fromtimestamp(data["synced_at"], ET).strftime("%m-%d %H:%M"), len(days),
                    days[0].strftime("%m-%d") if days else "?", days[-1].strftime("%m-%d") if days else "?",
                    len(lv), sum(o["shares_filled"] * o["avg_price"] + (o["fee"] or 0) for o in lv),
                    len(opn) - len(lv), time.time() - t0))
        notes = [
            "Each cell: profit per share after the taker fee in percentage points (+- 1 s.e. over fills), "
            "settled fills, P&L in dollars, ROI (P&L / dollars spent incl. fee). Sub-lines split the same "
            "fills by market type; 'paper twin' is the paper arm of the same name over the same days - "
            "live minus twin is the cost of real execution (the fill tax).",
            "FORWARD record only: actual live fills (real money) and actual paper fills (the paper bot buys "
            "against the book it fetched at the moment it decided, no depth beyond that book). No backtest "
            "or simulation number is shown here.",
            "Periods are the last N trading days WITH SETTLEMENTS, by the market's settlement date (ET), "
            "ending with the latest settled day. Positions still open are not counted (see the line above).",
            "N/A: the arm was not trading before the window's first settlement day, or there are fewer "
            "settled days than the window - no number is shown from any other source. All history is the "
            "arm's whole record, labelled with its first order.",
            "Paper arms that buy the same markets overlap: their numbers are not independent of each other.",
            "Verdict (from the 7- and 14-day windows, N/A until the arm has run through both): "
            "'live, losing over 7d' = the live arm lost money over its last 7 trading days.",
            "Rules are read from polyfin/live/config.py on the laptop - keep it at the deployed commit.",
        ]
        path = os.path.join(OUT, "arm_report.html")
        render(rows, path, dict(line=line, notes=notes))
        shutil.copy(path, os.path.join(OUT, "arm_report_%s.html" % day))
        log("wrote %s; live losing over 7d: %s; candidates: %s" % (path, bad or "none", cand or "none"))
        if not a.no_notify:
            notify("Polyfin arm report", ("LOSING 7d: %s" % ", ".join(bad)) if bad else "No live arm losing over 7d")
        return 0
    except Exception as e:
        log("FAILED: %r" % e)
        if not a.no_notify:
            notify("Polyfin arm report FAILED", str(e)[:200])
        raise


if __name__ == "__main__":
    sys.exit(main())
