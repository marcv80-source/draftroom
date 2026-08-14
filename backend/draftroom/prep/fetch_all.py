"""CLI: run every currently-configured prep source, cache raw, print a summary.

    python -m draftroom.prep.fetch_all

Runs against the live internet. Sources with no credentials configured
(FantasyPros, until an API key exists) are skipped with a clear line rather
than failing the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from draftroom.prep import fantasypros_client, ffc_client, sleeper_client
from draftroom.prep.fantasypros_client import NotConfiguredError


@dataclass
class SourceResult:
    source: str
    url: str
    status: str
    rows: str
    nonzero: str
    note: str = ""


def _status_from_exc(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc.response.status_code)
    return f"ERROR: {type(exc).__name__}"


def run_sleeper_players() -> SourceResult:
    try:
        raw = sleeper_client.fetch_players()
    except Exception as exc:  # noqa: BLE001
        return SourceResult("sleeper_players", sleeper_client.PLAYERS_URL, _status_from_exc(exc), "-", "-", note=str(exc)[:120])

    filtered = sleeper_client.filter_active_skill_players(raw)
    return SourceResult(
        source="sleeper_players",
        url=sleeper_client.PLAYERS_URL,
        status="200",
        rows=str(len(raw)),
        nonzero=str(len(filtered)),
        note="rows=total fetched, nonzero=active QB/RB/WR/TE after filter",
    )


def run_sleeper_projections(season: int) -> SourceResult:
    try:
        url_used, raw = sleeper_client.fetch_projections(season)
    except Exception as exc:  # noqa: BLE001
        guess_url = sleeper_client.PROJECTIONS_URL_PRIMARY.format(season=season)
        return SourceResult("sleeper_projections", guess_url, _status_from_exc(exc), "-", "-", note=str(exc)[:200])

    statlines = sleeper_client.to_statlines(raw)
    nonzero = sum(1 for sl in statlines.values() if sl.has_nonzero_stats())
    return SourceResult(
        source="sleeper_projections",
        url=url_used,
        status="200",
        rows=str(len(raw)),
        nonzero=str(nonzero),
        note="nonzero = StatLines with >=1 nonzero component stat",
    )


def run_ffc(teams: int = 12, year: int = 2026) -> SourceResult:
    url = ffc_client.BASE_URL.format(fmt="2qb", teams=teams, year=year)
    try:
        raw = ffc_client.fetch_adp(fmt="2qb", teams=teams, year=year)
        rows = ffc_client.parse_adp_rows(raw)
    except Exception as exc:  # noqa: BLE001
        return SourceResult("ffc_adp", url, _status_from_exc(exc), "-", "-", note=str(exc)[:200])

    has_stdev = all(r.std_dev is not None for r in rows) and len(rows) > 0
    try:
        passed, qbs_top20 = ffc_client.check_is_2qb_format(rows)
        sanity_note = f"2QB sanity PASS: {len(qbs_top20)} QBs in top 20 ({', '.join(qbs_top20[:6])}{'...' if len(qbs_top20) > 6 else ''})"
    except AssertionError as exc:
        sanity_note = f"2QB sanity FAIL: {exc}"

    return SourceResult(
        source="ffc_adp",
        url=url,
        status="200",
        rows=str(len(rows)),
        nonzero="-",
        note=f"std_dev field present (as 'stdev'): {has_stdev}. {sanity_note}",
    )


def run_fantasypros() -> SourceResult:
    if not fantasypros_client.SECRETS_PATH.exists():
        return SourceResult(
            "fantasypros",
            fantasypros_client.URL_TEMPLATE,
            "SKIPPED",
            "-",
            "-",
            note=f"no API key configured at {fantasypros_client.SECRETS_PATH}",
        )
    try:
        fantasypros_client._load_api_key()
    except NotConfiguredError as exc:
        return SourceResult("fantasypros", fantasypros_client.URL_TEMPLATE, "SKIPPED", "-", "-", note=str(exc)[:150])
    # A key exists but we've never validated the endpoint shape against it here;
    # leave that to --probe so a human eyeballs the first real response.
    return SourceResult(
        "fantasypros",
        fantasypros_client.URL_TEMPLATE,
        "NOT RUN",
        "-",
        "-",
        note="API key found but endpoint is unverified -- run "
        "`python -m draftroom.prep.fantasypros_client --probe` first",
    )


def _print_table(results: list[SourceResult]) -> None:
    headers = ["source", "url", "status", "rows", "nonzero"]
    col_widths = [max(len(h), *(len(getattr(r, h)) for r in results)) for h in headers]
    def fmt_row(values: list[str]) -> str:
        return "  ".join(v.ljust(w) for v, w in zip(values, col_widths))
    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in col_widths]))
    for r in results:
        print(fmt_row([r.source, r.url, r.status, r.rows, r.nonzero]))
        if r.note:
            print(f"    note: {r.note}")


def main() -> int:
    season = 2026
    results = [
        run_sleeper_players(),
        run_sleeper_projections(season),
        run_ffc(teams=12, year=season),
        run_fantasypros(),
    ]
    _print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
