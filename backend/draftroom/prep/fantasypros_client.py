"""FantasyPros API client -- ABANDONED. See prep/manual_csv.py instead.

Marc decided against API integration (2026-08-17): FantasyPros API access
needs manual approval, premium API keys require a $22.99/mo HOF
subscription, and the free tier may return only sample data. The
projections pages are public and have a CSV export button, so FantasyPros
projections are now ingested by hand as weekly CSV downloads via
prep/manual_csv.py -- see docs/MANUAL_PROJECTIONS.md for the exact steps
Marc follows.

This module is gutted, not deleted, for one reason: tests/test_data_layer.py
(owned by another workstream, not this one) still exercises the contract
"no key configured -> loud NotConfiguredError, never touches the network".
That contract is worth keeping as a permanent tripwire even though the API
path itself is abandoned, so `NotConfiguredError` and `fetch_projections`
still exist here -- `fetch_projections` now *always* raises, unconditionally,
regardless of whether a key file exists.

Do NOT add back a live fetch here, and do NOT scrape fantasypros.com either.
No HTTP calls to fantasypros.com anywhere in this pipeline -- see
prep/manual_csv.py's module docstring for why.
"""

from __future__ import annotations


class NotConfiguredError(RuntimeError):
    """Raised by every call into this module.

    Historically meant "no API key is set up yet"; now permanent, because the
    FantasyPros API integration was deliberately abandoned in favor of manual
    CSV ingest (see prep/manual_csv.py). Kept as a distinct class (not
    deleted, not folded into a generic exception) because
    tests/test_data_layer.py asserts this exact type.
    """


def fetch_projections(position: str, season: int = 2026) -> dict:
    """Always raises NotConfiguredError. Never makes an HTTP request.

    The FantasyPros API is not used anywhere in this pipeline -- see
    prep/manual_csv.py and docs/MANUAL_PROJECTIONS.md for the real,
    manual-CSV ingest path.
    """
    raise NotConfiguredError(
        "FantasyPros API integration was deliberately abandoned in favor of "
        "manual CSV ingest (Marc's call, 2026-08-17: manual approval + a "
        "$22.99/mo premium key beat maintaining an authenticated client). "
        "See prep/manual_csv.py and docs/MANUAL_PROJECTIONS.md. This "
        "function will never fetch live -- do not add that back."
    )
