#!/usr/bin/env python3
"""Move pUSD between the EOA and the deposit-wallet proxy.

Ported from polycrypto (scripts/move_funds.py, doc/how_to_move_funds.md).  The
CLOB draws collateral from the proxy (the funder), so pUSD in the EOA is inert.
The two directions are different mechanisms:

    eoa-to-proxy   plain ERC-20 transfer signed and broadcast by the EOA.
                   The EOA pays gas and needs POL.
    proxy-to-eoa   the proxy cannot sign: a DepositWallet.Batch through
                   Polymarket's relayer, gasless.  Same path as redemption.

Guards: refuses unless the proxy's on-chain owner() is our EOA (the proxy is
read from FIN_DEPOSIT_WALLET, never derived - the factory changed and a derived
address can be an empty phantom that accepts the transfer); amounts are pUSD,
converted to 6-decimal base units here; success is judged by balanceOf before
and after, not by the receipt.

    .venv/bin/python scripts/move_funds.py --direction eoa-to-proxy --amount 1          # dry run
    .venv/bin/python scripts/move_funds.py --direction eoa-to-proxy --amount 1 --send
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.live import chain as CH  # noqa: E402
from polyfin.settings import Credentials  # noqa: E402

DECIMALS = 6


def erc20_transfer(to: str, base_units: int) -> bytes:
    """transfer(address,uint256) - amount in BASE UNITS (1 pUSD = 1_000_000)."""
    return CH._keccak(b"transfer(address,uint256)")[:4] + CH._addr_word(to) + CH._w(base_units)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--direction", required=True, choices=("eoa-to-proxy", "proxy-to-eoa"))
    ap.add_argument("--amount", type=float, required=True, help="in pUSD")
    ap.add_argument("--send", action="store_true", help="actually send; default is a dry run")
    a = ap.parse_args()

    c = Credentials()
    if not c.private_key or not c.funder:
        sys.exit("need FIN_PRIVATE_KEY and FIN_DEPOSIT_WALLET")
    from eth_account import Account
    ch = CH.Chain(c.rpc_url, c.funder)
    w3 = ch.w3
    eoa = Account.from_key(c.private_key).address
    proxy = ch.holder
    units = int(round(a.amount * 10 ** DECIMALS))

    owner = "0x" + w3.eth.call({"to": proxy, "data": "0x8da5cb5b"}).hex()[-40:]
    print(f"  chain {w3.eth.chain_id}  block {w3.eth.block_number}")
    print(f"  proxy owner() {owner}  {'== EOA' if owner.lower() == eoa.lower() else '!= EOA'}")
    if owner.lower() != eoa.lower():
        sys.exit("  REFUSING: FIN_DEPOSIT_WALLET is not owned by this EOA")

    def pusd(who):
        return ch.pusd.functions.balanceOf(w3.to_checksum_address(who)).call() / 1e6

    def pol(who):
        return w3.eth.get_balance(w3.to_checksum_address(who)) / 1e18

    p0, e0 = pusd(proxy), pusd(eoa)
    print(f"  EOA   {eoa}  {e0:12.6f} pUSD  {pol(eoa):.6f} POL")
    print(f"  PROXY {proxy}  {p0:12.6f} pUSD  {pol(proxy):.6f} POL")
    src, dst = (proxy, eoa) if a.direction == "proxy-to-eoa" else (eoa, proxy)
    print(f"\n  moving {a.amount} pUSD ({units} base units) {src} -> {dst}")
    have = p0 if src == proxy else e0
    if have < a.amount:
        sys.exit(f"  source holds only {have:.6f} pUSD - refusing")
    data = erc20_transfer(dst, units)
    print(f"  calldata {data.hex()[:10]}... ({len(data)} bytes)")

    if a.direction == "proxy-to-eoa":
        print("  path: relayer DepositWallet.Batch (gasless)")
        if not c.relayer_api_key or not c.relayer_api_key_address:
            sys.exit("  needs FIN_RELAYER_API_KEY(_ADDRESS)")
        if not a.send:
            print("\n  DRY RUN - nothing sent.  Re-run with --send.")
            return 0
        rl = CH.Relayer(c.relayer_api_key, c.relayer_api_key_address)
        rec = rl.submit(c.private_key, proxy, [{"target": CH.PUSD, "value": 0, "data": data}])
        tid = rec.get("transactionID") or rec.get("transactionId")
        print(f"  submitted, relayer id {tid}")
        conf = rl.wait_confirmed(tid)
        print(f"  confirmed tx {conf.get('transactionHash')}")
    else:
        print("  path: direct ERC-20 transfer signed by the EOA (EOA pays gas)")
        if pol(eoa) <= 0:
            sys.exit("  the EOA holds no POL for gas - refusing")
        if not a.send:
            print("\n  DRY RUN - nothing sent.  Re-run with --send.")
            return 0
        tx = {"to": w3.to_checksum_address(CH.PUSD), "data": data, "value": 0, "from": eoa,
              "chainId": 137, "nonce": w3.eth.get_transaction_count(eoa)}
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.3)
        tx["maxFeePerGas"] = w3.eth.gas_price * 2
        tx["maxPriorityFeePerGas"] = w3.to_wei(30, "gwei")
        h = w3.eth.send_raw_transaction(Account.from_key(c.private_key).sign_transaction(tx)
                                        .raw_transaction)
        print(f"  sent tx 0x{h.hex().removeprefix('0x')}")
        r = w3.eth.wait_for_transaction_receipt(h, timeout=180)
        print(f"  mined block {r.blockNumber}, status {r.status}, gas {r.gasUsed}")
        if r.status != 1:
            sys.exit("  transaction REVERTED")

    p1, e1 = pusd(proxy), pusd(eoa)
    print(f"\n  proxy {p0:12.6f} -> {p1:12.6f}  ({p1 - p0:+.6f})")
    print(f"  EOA   {e0:12.6f} -> {e1:12.6f}  ({e1 - e0:+.6f})")
    moved = (e1 - e0) if dst == eoa else (p1 - p0)
    ok = abs(moved - a.amount) < 1e-6
    print(f"\n  {'OK' if ok else 'UNEXPECTED'}: destination changed by {moved:+.6f}, "
          f"expected {a.amount:+.6f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
