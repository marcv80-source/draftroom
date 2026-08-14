"""Shared HTTP client, raw-response caching, and offline replay for prep sources.

Raw fetches always land on disk under data/raw/<source>/ before anything parses
them, per CLAUDE.md. Tests and offline runs read back via load_latest_raw and
never touch the network.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# backend/draftroom/prep/http.py -> parents[3] == repo root (C:\dev\draftroom)
REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_DATA_ROOT = REPO_ROOT / "data" / "raw"

USER_AGENT = (
    "draftroom/0.1 (+personal fantasy football draft tool; "
    "contact: marc.valdes@domainrealestatepartners.com)"
)

DEFAULT_TIMEOUT = httpx.Timeout(30.0)


def _resolve_default_verify() -> str | bool:
    """Pick a CA bundle to verify against.

    This machine sits behind a TLS-inspecting corporate proxy: the public CA
    bundle httpx/certifi ships with does not include the corp root, so plain
    `httpx.Client()` fails every HTTPS request with CERTIFICATE_VERIFY_FAILED.
    Node CLIs on this box already work around this via NODE_EXTRA_CA_CERTS
    pointing at an exported Windows CA bundle; reuse the same bundle here.
    Falls back to httpx's normal (certifi) verification if none of these exist,
    so this is harmless on a machine without that proxy.
    """
    candidates = [
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("NODE_EXTRA_CA_CERTS"),
        r"C:\Users\mvaldes\.claude\corp-ca-bundle.pem",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return True


DEFAULT_VERIFY: str | bool = _resolve_default_verify()

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TransportError,
)


def make_client(**kwargs: Any) -> httpx.Client:
    """A shared httpx.Client with a sane timeout and a descriptive User-Agent."""
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", {}) or {})
    kwargs.setdefault("verify", DEFAULT_VERIFY)
    return httpx.Client(timeout=DEFAULT_TIMEOUT, headers=headers, follow_redirects=True, **kwargs)


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """Request with exponential backoff on 429/5xx responses and connect errors.

    Retries `attempts` times total (default 3). Backoff is backoff_seconds * 2**n.
    """
    last_exc: Exception | None = None
    response: httpx.Response | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt == attempts:
                raise
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))
            continue

        if response.status_code in RETRYABLE_STATUS and attempt < attempts:
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))
            continue
        return response

    if last_exc is not None:
        raise last_exc
    assert response is not None  # pragma: no cover - unreachable
    return response


def get_json(client: httpx.Client, url: str, *, attempts: int = 3, **kwargs: Any) -> Any:
    """GET url with retry and return the parsed JSON body. Raises on non-2xx."""
    resp = request_with_retry(client, "GET", url, attempts=attempts, **kwargs)
    resp.raise_for_status()
    return resp.json()


def cache_raw(source: str, payload: Any, suffix: str = "json") -> Path:
    """Write payload to data/raw/<source>/<UTC ISO timestamp>.<suffix>.

    Creates the source directory if needed. Returns the path written.
    JSON-serializable payloads (dict/list/etc.) are written with json.dumps;
    bytes are written as-is; anything else is str()'d.
    """
    out_dir = RAW_DATA_ROOT / source
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
    path = out_dir / f"{ts}.{suffix}"

    if suffix == "json":
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    elif isinstance(payload, (bytes, bytearray)):
        path.write_bytes(payload)
    else:
        path.write_text(str(payload), encoding="utf-8")

    return path


def load_latest_raw(source: str) -> Any:
    """Read back the newest cached payload for `source`.

    Filenames are UTC ISO timestamps, which sort lexically in chronological
    order, so the last name in sorted order is the newest.
    """
    out_dir = RAW_DATA_ROOT / source
    if not out_dir.exists():
        raise FileNotFoundError(f"no cached raw data for source '{source}' at {out_dir}")

    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    if not files:
        raise FileNotFoundError(f"no cached raw files for source '{source}' in {out_dir}")

    latest = files[-1]
    if latest.suffix == ".json":
        return json.loads(latest.read_text(encoding="utf-8"))
    return latest.read_text(encoding="utf-8")
