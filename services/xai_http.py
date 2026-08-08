"""Shared HTTPS client for api.x.ai.

macOS python.org builds often ship with an empty default CA store
(CERTIFICATE_VERIFY_FAILED). Prefer certifi's bundle when available.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Any

import config


def ssl_context() -> ssl.SSLContext:
    """TLS context that works on stock macOS Python installs."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        # Fall back to system defaults (may still fail on broken installs).
        cafile = os.environ.get("SSL_CERT_FILE")
        if cafile:
            return ssl.create_default_context(cafile=cafile)
        return ssl.create_default_context()


def _api_key() -> str:
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    return key


def post_raw(path: str, payload: dict[str, Any], timeout: int = 60) -> bytes:
    """POST JSON to config.API_BASE + path; return raw response body."""
    request = urllib.request.Request(
        config.API_BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read()[:600].decode(errors="replace")
        raise RuntimeError(f"xAI {path} returned {error.code}: {body}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"xAI {path} failed: {error}") from error


def post_json(path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """POST JSON and parse a JSON object response."""
    raw = post_raw(path, payload, timeout=timeout)
    return json.loads(raw)
