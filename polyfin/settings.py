"""Environment loading.  Real env vars win over `.env`; values are never logged."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loaded = False


def load_env(path: Path = ROOT / ".env") -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    v = os.environ.get(f"FIN_{name}", default)
    return None if v is None or "CHANGE_ME" in v else v


def pg_dsn() -> str:
    parts = {
        "host": env("PGHOST", "127.0.0.1"),
        "port": env("PGPORT", "5446"),
        "user": env("PGUSER", "polyfin"),
        "dbname": env("PGDATABASE", "polyfin"),
        "password": env("PGPASSWORD"),
    }
    if not parts["password"]:
        raise RuntimeError("FIN_PGPASSWORD is not set (copy .env.example to .env)")
    return " ".join(f"{k}={v}" for k, v in parts.items())


class Credentials:
    """The FIN_ wallet.  Only live trading and the wallet scripts read this."""

    TRADING = ("PRIVATE_KEY", "DEPOSIT_WALLET", "API_KEY", "API_SECRET", "API_PASSPHRASE")

    def __init__(self):
        self.private_key = env("PRIVATE_KEY")
        self.funder = env("DEPOSIT_WALLET")
        self.signature_type = int(env("SIGNATURE_TYPE", "3") or 3)
        self.api_key = env("API_KEY")
        self.api_secret = env("API_SECRET")
        self.api_passphrase = env("API_PASSPHRASE")
        self.rpc_url = env("POLYGON_RPC_URL")
        self.relayer_api_key = env("RELAYER_API_KEY")
        self.relayer_api_key_address = env("RELAYER_API_KEY_ADDRESS")

    @property
    def missing_for_trading(self) -> list[str]:
        vals = {"PRIVATE_KEY": self.private_key, "DEPOSIT_WALLET": self.funder,
                "API_KEY": self.api_key, "API_SECRET": self.api_secret,
                "API_PASSPHRASE": self.api_passphrase}
        return [f"FIN_{k}" for k in self.TRADING if not vals[k]]

    @property
    def api_creds(self) -> dict:
        return {"api_key": self.api_key, "api_secret": self.api_secret,
                "api_passphrase": self.api_passphrase}
