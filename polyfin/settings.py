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
