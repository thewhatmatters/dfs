"""urllib GET helpers with certifi SSL. Never log query strings (Odds apiKey)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "dfs-nfl/0.1 (fanduel-classic)"


class HttpError(Exception):
    """Fatal HTTP."""


class HttpAuthError(HttpError):
    gate = "LINES_AUTH"


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def http_json(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: int = 30,
) -> tuple[Any, dict[str, str]]:
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            body = resp.read().decode("utf-8")
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except ssl.SSLError as e:
        raise HttpError(
            f"TLS verify failed ({e}). Install certifi (`python3 -m pip install certifi`) "
            "or run Python's Install Certificates.command"
        ) from e
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in {401, 403}:
            raise HttpAuthError(f"HTTP {e.code} (key rejected). {payload}") from e
        raise HttpError(f"HTTP {e.code}: {payload}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"unreachable: {e.reason}") from e
    try:
        return json.loads(body), resp_hdrs
    except json.JSONDecodeError as e:
        raise HttpError(f"non-JSON: {e}") from e


def http_json_post(
    url: str,
    body: object,
    headers: dict[str, str] | None = None,
    *,
    timeout: int = 60,
) -> tuple[Any, dict[str, str]]:
    """POST a JSON body. Do not put secrets in `url` or `body`."""
    data = json.dumps(body).encode("utf-8")
    hdrs = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            raw = resp.read().decode("utf-8")
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except ssl.SSLError as e:
        raise HttpError(
            f"TLS verify failed ({e}). Install certifi (`python3 -m pip install certifi`) "
            "or run Python's Install Certificates.command"
        ) from e
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in {401, 403}:
            raise HttpAuthError(f"HTTP {e.code} (key rejected). {payload}") from e
        raise HttpError(f"HTTP {e.code}: {payload}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"unreachable: {e.reason}") from e
    try:
        return json.loads(raw), resp_hdrs
    except json.JSONDecodeError as e:
        raise HttpError(f"non-JSON: {e}") from e


def http_text(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: int = 45,
) -> tuple[str, dict[str, str]]:
    """GET response body as text (HTML pages, etc.)."""
    hdrs = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            resp_hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except ssl.SSLError as e:
        raise HttpError(
            f"TLS verify failed ({e}). Install certifi (`python3 -m pip install certifi`) "
            "or run Python's Install Certificates.command"
        ) from e
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", errors="replace")[:300]
        # 403 on HTML is often Cloudflare, not an API key rejection.
        raise HttpError(f"HTTP {e.code}: {payload}") from e
    except urllib.error.URLError as e:
        raise HttpError(f"unreachable: {e.reason}") from e
    return body, resp_hdrs
