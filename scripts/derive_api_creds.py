#!/usr/bin/env python3
"""Derive the CLOB L2 API credentials from FIN_PRIVATE_KEY and write them to .env.

Ported from polycrypto.  The three FIN_API_* values are derived
deterministically from the EOA's signature (`create_or_derive_api_key` creates
them on first use and returns the same ones afterwards), so this is safe to
re-run.  They authenticate REST calls; they do not sign orders.  They are still
a secret: they can read positions and cancel orders.

    .venv/bin/python scripts/derive_api_creds.py            # show, write nothing
    .venv/bin/python scripts/derive_api_creds.py --write    # update .env (mode 600)

Needs FIN_PRIVATE_KEY and FIN_DEPOSIT_WALLET in .env first.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polyfin.live.clob import CHAIN_ID, HOST  # noqa: E402
from polyfin.settings import ROOT, Credentials  # noqa: E402

KEYS = ("FIN_API_KEY", "FIN_API_SECRET", "FIN_API_PASSPHRASE")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="update .env")
    a = ap.parse_args()
    c = Credentials()
    if not c.private_key or not c.funder:
        sys.exit("set FIN_PRIVATE_KEY and FIN_DEPOSIT_WALLET in .env first")

    from eth_account import Account
    from py_clob_client_v2.client import ClobClient
    print(f"  deriving for EOA {Account.from_key(c.private_key).address}")
    client = ClobClient(HOST, chain_id=CHAIN_ID, key=c.private_key,
                        signature_type=c.signature_type, funder=c.funder)
    creds = client.create_or_derive_api_key()
    if not creds or not creds.api_key:
        sys.exit("the CLOB returned no credentials")
    vals = dict(zip(KEYS, (creds.api_key, creds.api_secret, creds.api_passphrase)))
    for k, v in vals.items():
        print(f"  {k:<20} {v[:6]}...{v[-4:]}  ({len(v)} chars)")

    # prove they authenticate before writing them
    check = ClobClient(HOST, chain_id=CHAIN_ID, key=c.private_key, creds=creds,
                       signature_type=c.signature_type, funder=c.funder)
    try:
        keys = check.get_api_keys()
        print(f"  L2 verified: {len(keys.get('apiKeys', []))} key(s) on this account")
    except Exception as e:
        sys.exit(f"  derived, but L2 auth FAILED: {str(e)[:160]}")

    if not a.write:
        print("\n  not written - re-run with --write")
        return 0
    path = ROOT / ".env"
    lines = path.read_text().splitlines() if path.exists() else []
    seen, out = set(), []
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line else ""
        if k in vals:
            out.append(f"{k}={vals[k]}")
            seen.add(k)
        else:
            out.append(line)
    out += [f"{k}={vals[k]}" for k in KEYS if k not in seen]
    path.write_text("\n".join(out) + "\n")
    os.chmod(path, 0o600)
    print(f"\n  wrote {len(vals)} values to {path} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
