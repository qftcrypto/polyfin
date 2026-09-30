"""On-chain side of a signature-type-3 deposit wallet: reads, and relayer calls.

Ported from polycrypto (live/relayer.py, live/redeem.py), which runs this in
production.  Facts that took polycrypto time to establish:

* Positions and pUSD live in the DEPOSIT WALLET, not the EOA.  An EOA-direct
  `redeemPositions` burns nothing (it burns msg.sender's balance), so every
  write goes through Polymarket's relayer as a DepositWallet.Batch signed by
  the EOA; the relayer pays gas.
* Collateral is pUSD.  Passing USDC.e to redeemPositions redeems nothing and
  does not revert.
* Two approvals are needed, both FROM the deposit wallet:
    pUSD.approve(CTF Exchange V2, max)            - or every BUY fails "not enough allowance"
    CTF.setApprovalForAll(AutoRedeemOperator, true) - or winners are never auto-redeemed
  All polyfin markets are negRisk=false, so the V2 exchange (not the NegRisk
  adapter) is the spender.
* Cloudflare fronts the relayer and answers urllib's default User-Agent with
  a 403 that looks exactly like a bad API key.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("chain")

POLYGON = 137
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
AUTO_REDEEM_OPERATOR = "0xf3cfb6a6ebfeb51876289eb235719eb1c65252b0"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
RELAYER_URL = "https://relayer-v2.polymarket.com"
UA = "polyfin/0.1"
MAX_UINT = 2 ** 256 - 1
DEFAULT_RPCS = ["https://polygon-rpc.com", "https://polygon-bor-rpc.publicnode.com",
                "https://1rpc.io/matic"]

TERMINAL_OK = "STATE_CONFIRMED"
TERMINAL_BAD = ("STATE_FAIL", "STATE_REJECTED", "STATE_CANCELLED")


# -- calldata -----------------------------------------------------------------------
def _keccak(b: bytes) -> bytes:
    from eth_utils import keccak
    return keccak(b)


def _w(x: int) -> bytes:
    return x.to_bytes(32, "big")


def _addr_word(a: str) -> bytes:
    from eth_utils import to_bytes, to_checksum_address
    return b"\x00" * 12 + to_bytes(hexstr=to_checksum_address(a))


def _b32(h: str) -> bytes:
    from eth_utils import to_bytes
    b = to_bytes(hexstr=h)
    if len(b) != 32:
        raise ValueError(f"expected 32 bytes, got {len(b)} from {h!r}")
    return b


def redeem_calldata(condition_id: str, collateral: str = PUSD) -> bytes:
    """redeemPositions(collateral, 0x0, conditionId, [1, 2]) - both slots always:
    redeeming a zero balance is harmless, so we need not know which side won."""
    sel = _keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    return (sel + _addr_word(collateral) + _w(0) + _b32(condition_id)
            + _w(0x80) + _w(2) + _w(1) + _w(2))


def approve_calldata(spender: str, amount: int = MAX_UINT) -> bytes:
    return _keccak(b"approve(address,uint256)")[:4] + _addr_word(spender) + _w(amount)


def set_approval_for_all_calldata(operator: str, approved: bool = True) -> bytes:
    return (_keccak(b"setApprovalForAll(address,bool)")[:4] + _addr_word(operator)
            + _w(1 if approved else 0))


def batch_digest(chain_id: int, deposit_wallet: str, nonce: int, deadline: int,
                 calls: list[dict]) -> bytes:
    """EIP-712 digest of DepositWallet.Batch (typehashes from polycrypto/polycopy)."""
    dom_t = _keccak(b"EIP712Domain(string name,string version,uint256 chainId,"
                    b"address verifyingContract)")
    call_t = _keccak(b"Call(address target,uint256 value,bytes data)")
    batch_t = _keccak(b"Batch(address wallet,uint256 nonce,uint256 deadline,Call[] calls)"
                      b"Call(address target,uint256 value,bytes data)")
    domain = _keccak(dom_t + _keccak(b"DepositWallet") + _keccak(b"1") + _w(chain_id)
                     + _addr_word(deposit_wallet))
    inner = b"".join(_keccak(call_t + _addr_word(c["target"]) + _w(c["value"])
                             + _keccak(c["data"])) for c in calls)
    struct = _keccak(batch_t + _addr_word(deposit_wallet) + _w(nonce) + _w(deadline)
                     + _keccak(inner))
    return _keccak(b"\x19\x01" + domain + struct)


# -- relayer -----------------------------------------------------------------------
class Relayer:
    def __init__(self, api_key: str, api_key_address: str, base_url: str = RELAYER_URL,
                 timeout: float = 20.0):
        if not api_key or not api_key_address:
            raise ValueError("relayer needs FIN_RELAYER_API_KEY and "
                             "FIN_RELAYER_API_KEY_ADDRESS (polymarket.com/settings)")
        self.api_key, self.api_key_address = api_key, api_key_address
        self.base, self.timeout = base_url.rstrip("/"), timeout

    def _req(self, path: str, body: dict | None = None):
        req = urllib.request.Request(
            self.base + path, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"RELAYER_API_KEY": self.api_key,
                     "RELAYER_API_KEY_ADDRESS": self.api_key_address,
                     "Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body_txt = e.read()[:300].decode(errors="replace")
            raise RuntimeError(f"relayer {path.split('?')[0]} HTTP {e.code}: {body_txt}") from e

    def wallet_nonce(self, owner: str) -> int:
        return int(self._req(f"/nonce?address={owner.lower()}&type=WALLET")["nonce"])

    def submit(self, private_key: str, deposit_wallet: str, calls: list[dict],
               deadline_s: int = 600) -> dict:
        from eth_account import Account
        acct = Account.from_key(private_key)
        nonce = self.wallet_nonce(acct.address)       # always fresh
        deadline = int(time.time()) + deadline_s
        digest = batch_digest(POLYGON, deposit_wallet, nonce, deadline, calls)
        sign = getattr(Account, "unsafe_sign_hash", None) or Account._sign_hash
        sig = sign(digest, private_key=private_key)
        raw = (int(sig.r).to_bytes(32, "big") + int(sig.s).to_bytes(32, "big")
               + bytes([sig.v if sig.v in (27, 28) else sig.v + 27]))
        return self._req("/submit", {
            "type": "WALLET", "from": acct.address.lower(), "to": DEPOSIT_WALLET_FACTORY,
            "nonce": str(nonce), "signature": "0x" + raw.hex(),
            "depositWalletParams": {
                "depositWallet": deposit_wallet.lower(), "deadline": str(deadline),
                "calls": [{"target": c["target"].lower(), "value": str(c["value"]),
                           "data": "0x" + c["data"].hex()} for c in calls]}})

    def wait_confirmed(self, transaction_id: str, tries: int = 20, every: float = 3.0) -> dict:
        for _ in range(tries):
            rec = self._req(f"/transaction?id={transaction_id}")
            rec = (rec[0] if rec else {}) if isinstance(rec, list) else rec
            state = rec.get("state", "")
            if state == TERMINAL_OK:
                return rec
            if state in TERMINAL_BAD:
                raise RuntimeError(f"relayer tx {transaction_id} {state}")
            time.sleep(every)
        raise TimeoutError(f"relayer tx {transaction_id} never confirmed")

    def run(self, private_key: str, deposit_wallet: str, calls: list[dict]) -> dict:
        rec = self.submit(private_key, deposit_wallet, calls)
        tid = rec.get("transactionID") or rec.get("transactionId")
        return self.wait_confirmed(tid) if tid else rec

    def redeem(self, private_key: str, deposit_wallet: str, condition_id: str) -> dict:
        return self.run(private_key, deposit_wallet,
                        [{"target": CTF, "value": 0, "data": redeem_calldata(condition_id)}])


# -- reads --------------------------------------------------------------------------
_ABI = json.loads("""[
 {"inputs":[{"name":"conditionId","type":"bytes32"}],"name":"payoutDenominator",
  "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"name":"owner","type":"address"},{"name":"id","type":"uint256"}],
  "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view",
  "type":"function"},
 {"inputs":[{"name":"owner","type":"address"},{"name":"operator","type":"address"}],
  "name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],
  "stateMutability":"view","type":"function"}]""")
_ERC20 = json.loads("""[
 {"inputs":[{"name":"a","type":"address"}],"name":"balanceOf",
  "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"name":"o","type":"address"},{"name":"s","type":"address"}],"name":"allowance",
  "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]""")


class Chain:
    """Read-only Polygon access for the deposit wallet."""

    def __init__(self, rpc_url: str | None, holder: str):
        from web3 import Web3
        urls = [u.strip() for u in (rpc_url or ",".join(DEFAULT_RPCS)).split(",") if u.strip()]
        self.w3, last = None, None
        for u in urls:
            try:
                w3 = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 20}))
                if w3.is_connected() and w3.eth.chain_id == POLYGON:
                    self.w3 = w3
                    break
                last = f"{u}: wrong chain or not connected"
            except Exception as e:
                last = f"{u}: {str(e)[:80]}"
        if self.w3 is None:
            raise RuntimeError(f"no usable Polygon RPC ({last})")
        cs = Web3.to_checksum_address
        self.holder = cs(holder)
        self.ctf = self.w3.eth.contract(address=cs(CTF), abi=_ABI)
        self.pusd = self.w3.eth.contract(address=cs(PUSD), abi=_ERC20)
        self._cs = cs

    def token_balance(self, token_id: str) -> float:
        return self.ctf.functions.balanceOf(self.holder, int(token_id)).call() / 1e6

    def is_resolved(self, condition_id: str) -> bool:
        return self.ctf.functions.payoutDenominator(_b32(condition_id)).call() > 0

    def pusd_balance(self) -> float:
        return self.pusd.functions.balanceOf(self.holder).call() / 1e6

    def pusd_allowance(self, spender: str = EXCHANGE_V2) -> int:
        return self.pusd.functions.allowance(self.holder, self._cs(spender)).call()

    def auto_redeem_approved(self) -> bool:
        return self.ctf.functions.isApprovedForAll(
            self.holder, self._cs(AUTO_REDEEM_OPERATOR)).call()
