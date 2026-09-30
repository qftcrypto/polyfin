#!/usr/bin/env python3
"""Verify the live setup end to end WITHOUT placing an order.

Ported from polycrypto.  Signature type 3 fails in ways that look like ordinary
rejections, so this separates the failure modes while nothing is at risk.  The
decisive check signs a real order locally and inspects it (maker == signer ==
deposit wallet, signatureType 3, ERC-7739-wrapped signature) - the only way to
verify that path short of sending money.  Nothing here posts, cancels or
transfers.

    .venv/bin/python scripts/preflight.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.config import GAMMA, SERIES  # noqa: E402
from polyfin.http import get_json  # noqa: E402
from polyfin.live import chain as CH  # noqa: E402
from polyfin.live.clob import CHAIN_ID, HOST  # noqa: E402
from polyfin.live.config import FEE_RATE  # noqa: E402
from polyfin.settings import Credentials  # noqa: E402

OK, BAD, WARN = "[ ok ]", "[FAIL]", "[warn]"
fails: list[str] = []


def check(cond, msg, detail=""):
    print(f"  {OK if cond else BAD} {msg}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(msg)
    return cond


def note(msg, detail=""):
    print(f"  {WARN} {msg}" + (f"  {detail}" if detail else ""))


def live_finance_market():
    """An open SPY up/down market, for market-specific checks."""
    sid = SERIES["spy-daily-up-or-down"][0]
    for ev in get_json(f"{GAMMA}/events?series_id={sid}&closed=false&limit=5"):
        for m in ev.get("markets") or []:
            if m.get("clobTokenIds") and not m.get("closed"):
                return json.loads(m["clobTokenIds"])[0], m
    return None, None


def main() -> int:
    c = Credentials()
    print("=== 1. credentials (FIN_*) ===")
    missing = c.missing_for_trading
    for k in ("FIN_" + x for x in c.TRADING):
        check(k not in missing, k)
    if missing:
        print("\n  fill these in .env (scripts/derive_api_creds.py makes the API_* ones)")
        return 1
    check(c.signature_type == 3, "signature type is 3 (POLY_1271)", f"got {c.signature_type}")
    note("FIN_POLYGON_RPC_URL " + ("set" if c.rpc_url else "unset - public RPCs, redemption only"))
    note("relayer key " + ("set" if c.relayer_api_key else "unset - no redeem fallback"))

    print("\n=== 2. client (v2 - v1 cannot sign type 3) ===")
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (ApiCreds, AssetType, BalanceAllowanceParams,
                                              OrderArgsV2)
    from py_clob_client_v2.config import get_contract_config
    client = ClobClient(HOST, chain_id=CHAIN_ID, key=c.private_key,
                        creds=ApiCreds(**c.api_creds), signature_type=3, funder=c.funder)
    eoa = client.get_address()
    print(f"       EOA (signs)     {eoa}\n       funder (holds)  {c.funder}")
    check(eoa.lower() != c.funder.lower(), "EOA and deposit wallet differ, as type 3 requires")

    print("\n=== 3. connectivity and clock ===")
    try:
        check(bool(client.get_ok()), "CLOB reachable")
        skew = int(client.get_server_time()) - int(time.time())
        check(abs(skew) < 30, "clock within 30s of the venue", f"skew {skew:+d}s")
    except Exception as e:
        check(False, "CLOB reachable", str(e)[:120])

    print("\n=== 4. L2 auth, balance, exchange allowance (per the CLOB) ===")
    cfg = get_contract_config(CHAIN_ID)
    try:
        ba = client.get_balance_allowance(BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL, signature_type=3))
        check(isinstance(ba, dict) and "balance" in ba, "L2 authenticated")
        bal = float(ba.get("balance", 0)) / 1e6
        check(bal > 0, "deposit wallet holds pUSD", f"{bal:,.2f}")
        # `allowances` is keyed by spender; the singular reads 0 for an approved wallet
        got = next((int(v or 0) for k, v in (ba.get("allowances") or {}).items()
                    if k.lower() == cfg.exchange_v2.lower()), 0)
        check(got > 0, "pUSD approved to CTF Exchange V2",
              "" if got else "needed for every BUY: scripts/approve.py")
    except Exception as e:
        check(False, "L2 authenticated", str(e)[:160])
    check(cfg.collateral.lower() == CH.PUSD.lower(), "collateral is pUSD", cfg.collateral)
    check(cfg.exchange_v2.lower() == CH.EXCHANGE_V2.lower(), "exchange_v2 address matches")

    print("\n=== 5. on chain (deposit wallet) ===")
    try:
        ch = CH.Chain(c.rpc_url, c.funder)
        owner = ch.w3.eth.call({"to": ch.holder, "data": "0x8da5cb5b"})   # owner()
        owner = "0x" + owner.hex()[-40:]
        check(owner.lower() == eoa.lower(), "deposit wallet owner() is our EOA",
              f"owner {owner}" + ("" if owner.lower() == eoa.lower() else
                                  " - wrong FIN_DEPOSIT_WALLET (do not re-derive it)"))
        check(ch.pusd_allowance() > 0, "on-chain pUSD allowance to Exchange V2")
        check(ch.auto_redeem_approved(), "CTF approved for the AutoRedeemOperator",
              "" if ch.auto_redeem_approved() else "winners will not auto-redeem: "
              "scripts/approve.py")
    except Exception as e:
        check(False, "on-chain reads", str(e)[:160])

    print("\n=== 6. a live finance market, and the decisive check: sign, post nothing ===")
    tok, mkt = live_finance_market()
    if not tok:
        note("no open SPY market found; skipping")
    else:
        check(not mkt.get("negRisk"), "market is not neg-risk (routes to exchange_v2)")
        rate = float((mkt.get("feeSchedule") or {}).get("rate", -1))
        check(abs(rate - FEE_RATE) < 1e-9, f"feeSchedule.rate is {FEE_RATE}", f"got {rate}")
        try:
            signed = client.create_order(OrderArgsV2(token_id=tok, price=0.05, size=5.0,
                                                     side="BUY"))
            d = signed.__dict__ if hasattr(signed, "__dict__") else {}
            sig = str(d.get("signature", ""))
            check(str(d.get("maker", "")).lower() == c.funder.lower(),
                  "maker is the deposit wallet")
            check(str(d.get("signer", "")).lower() == c.funder.lower(),
                  "signer is the deposit wallet (type 3 puts the funder here)")
            check(int(d.get("signatureType", -1)) == 3, "signatureType 3 on the wire")
            check(len(sig) > 200, "signature is ERC-7739-wrapped",
                  f"{len(sig)} chars" + ("" if len(sig) > 200 else " - plain EOA path taken"))
        except Exception as e:
            check(False, "order signs locally", f"{type(e).__name__}: {str(e)[:160]}")

    print()
    if fails:
        print(f"{len(fails)} check(s) FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("all checks passed - the live path is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
