"""Tiny HTTP helpers (standard library only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UA = {"User-Agent": "Mozilla/5.0"}  # Yahoo rejects full browser user-agents but accepts a plain one


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 20, retries: int = 2) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    return _call(urllib.request.Request(url, headers=UA), timeout, retries)


def post_json(url: str, body: Any, timeout: float = 20, retries: int = 2) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    return _call(req, timeout, retries)


def _call(req: urllib.request.Request, timeout: float, retries: int) -> Any:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1 + attempt)
                last = e
                continue
            raise HttpError(e.code, e.reason or "") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
    raise HttpError(0, f"unreachable ({type(last).__name__})")
