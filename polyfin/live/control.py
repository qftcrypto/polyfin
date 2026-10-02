"""Control row: pause / resume the trader without restarting it.

    python -m polyfin.live.control                 # show
    python -m polyfin.live.control pause "why"     # stop new entries
    python -m polyfin.live.control paper           # resume (paper)
    python -m polyfin.live.control live            # allow live, if launched with --live

Ratchet (from polycrypto): effective mode = min(launch authority, row), with
paused < paper < live.  The row can pause a process or allow live for one that
was launched with --live; it can never make a paper process trade live.
"""
from __future__ import annotations

import sys
import time

from ..db import connect

RANK = {"paused": 0, "paper": 1, "live": 2}


def read(conn) -> tuple[str, str | None]:
    row = conn.execute("SELECT mode, reason FROM trade.control WHERE id = 1").fetchone()
    conn.commit()
    return (row[0], row[1]) if row else ("paused", "control row missing")


ALIASES = {"pause": "paused", "stop": "paused", "resume": "paper"}


def set_mode(conn, mode: str, reason: str) -> None:
    mode = ALIASES.get(mode, mode)
    if mode not in RANK:
        raise ValueError(f"mode must be one of {list(RANK)}")
    conn.execute("UPDATE trade.control SET mode=%s, reason=%s, updated_at=%s WHERE id=1",
                 (mode, reason, int(time.time())))
    conn.commit()


def effective(conn, launch_mode: str) -> tuple[str, str | None]:
    row, reason = read(conn)
    mode = min(launch_mode, row, key=RANK.__getitem__)
    return mode, reason


def main() -> None:
    conn = connect()
    if len(sys.argv) > 1:
        set_mode(conn, sys.argv[1], " ".join(sys.argv[2:]) or "manual")
    mode, reason = read(conn)
    print(f"control: {mode} ({reason})")


if __name__ == "__main__":
    main()
