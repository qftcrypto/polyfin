#!/usr/bin/env python3
"""Set the two approvals a polyfin deposit wallet needs, through the relayer (gasless).

    pUSD.approve(CTF Exchange V2, max)              - every BUY needs it
    CTF.setApprovalForAll(AutoRedeemOperator, true) - winners auto-redeem

Both are calls FROM the deposit wallet, so they go through Polymarket's relayer
as one DepositWallet.Batch signed by the EOA.  Already-set approvals are
skipped.  Onboarding through the Polymarket website may have set them
already - run scripts/preflight.py first to see.

    .venv/bin/python scripts/approve.py           # show what would be sent
    .venv/bin/python scripts/approve.py --send    # send it

Needs FIN_PRIVATE_KEY, FIN_DEPOSIT_WALLET, FIN_RELAYER_API_KEY(_ADDRESS).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.live import chain as CH  # noqa: E402
from polyfin.settings import Credentials  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    c = Credentials()
    if not c.private_key or not c.funder:
        sys.exit("set FIN_PRIVATE_KEY and FIN_DEPOSIT_WALLET in .env")
    ch = CH.Chain(c.rpc_url, c.funder)
    calls = []
    if ch.pusd_allowance() > 0:
        print("  pUSD -> Exchange V2 allowance already set")
    else:
        calls.append({"target": CH.PUSD, "value": 0,
                      "data": CH.approve_calldata(CH.EXCHANGE_V2)})
        print("  will approve pUSD -> CTF Exchange V2 (unlimited)")
    if ch.auto_redeem_approved():
        print("  CTF -> AutoRedeemOperator approval already set")
    else:
        calls.append({"target": CH.CTF, "value": 0,
                      "data": CH.set_approval_for_all_calldata(CH.AUTO_REDEEM_OPERATOR)})
        print("  will setApprovalForAll CTF -> AutoRedeemOperator")
    if not calls:
        print("  nothing to do")
        return 0
    if not a.send:
        print("\n  not sent - re-run with --send")
        return 0
    rec = CH.Relayer(c.relayer_api_key, c.relayer_api_key_address).run(
        c.private_key, c.funder, calls)
    print(f"  confirmed: {rec.get('transactionHash') or rec}")
    print(f"  now: allowance {'set' if ch.pusd_allowance() > 0 else 'MISSING'}, "
          f"auto-redeem {'set' if ch.auto_redeem_approved() else 'MISSING'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
