"""Tiny JSON-over-HTTP helper with retries (stdlib only)."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("http")

# An honest client name.  A bare "Mozilla/5.0" without the rest of a browser's
# headers reads as a disguised bot: Cloudflare on clob.polymarket.com started
# returning 403 to it after ~1h of polling (2026-09-29); "polyfin/0.1" is accepted
# by CLOB, Gamma and Yahoo alike.
UA = "polyfin/0.1"


def get_json(url: str, body=None, retries: int = 3, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 4xx other than rate limiting will not get better by retrying
            if e.code != 429 and 400 <= e.code < 500:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as e:
            err = e
        wait = 2 ** attempt * (5 if getattr(err, "code", None) == 429 else 1)
        log.warning("GET %s failed (%s), retry in %ss", url[:120], err, wait)
        time.sleep(wait)
    raise err
