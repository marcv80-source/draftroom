"""CLI: run every currently-configured prep source, cache raw, print a summary.

    python -m draftroom.prep.fetch_all

Sleeper/FFC hit the live internet. FantasyPros is NOT fetched over the
network at all -- see prep/manual_csv.py -- it reads whatever CSVs Marc has
already downloaded into data/manual/. A position with no manual CSV yet (or
one that's missing/stale/wrong-season) degrades to a clearly-stated SKIPPED
or ERROR line rather than failing the whole run or silently pretending the
source is present; see run_manual_csv() below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from draftroom.prep import espn_client, ffc_client, manual_csv, sleeper_client


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


def run_espn_projections(season: int) -> SourceResult:
    """Fetch and map ESPN's season projections. See prep/espn_client.py for the
    endpoint, the verified stat-id mapping, and why rec_tgt (targets) matters:
    neither Sleeper nor FantasyPros carries that field at all."""
    url = espn_client.BASE_URL.format(season=season)
    try:
        url_used, raw = espn_client.fetch_projections(season)
    except Exception as exc:  # noqa: BLE001
        return SourceResult("espn_projections", url, _status_from_exc(exc), "-", "-", note=str(exc)[:200])

    statlines = espn_client.to_statlines(raw, season)
    nonzero = sum(1 for sl in statlines.values() if sl.has_nonzero_stats())
    has_targets = any(sl.rec_tgt > 0 for sl in statlines.values())
    return SourceResult(
        source="espn_projections",
        url=url_used,
        status="200",
        rows=str(len(raw)),
        nonzero=str(nonzero),
        note=f"nonzero = QB/RB/WR/TE StatLines with >=1 nonzero component stat. rec_tgt available: {has_targets}",
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


def run_manual_csv(position: str, season: int) -> SourceResult:
    """Load one position's manual FantasyPros CSV. Makes zero network calls.

    A missing file degrades gracefully to SKIPPED (the other sources still
    run -- see CLAUDE.md/task spec: "must degrade gracefully ... not crash
    and not silently pretend the source is present"). Every other failure
    (stale, wrong season, header drift, too few rows) is reported loudly as
    an ERROR line with the real exception message -- never downgraded to a
    warning, per the staleness-guard requirement.
    """
    source = f"manual_csv_{position}"
    try:
        result = manual_csv.load_position(position, season=season)
    except manual_csv.NoFileFoundError as exc:
        return SourceResult(
            source, str(manual_csv.MANUAL_DIR), "SKIPPED", "-", "-",
            note=str(exc)[:250],
        )
    except manual_csv.ManualCsvError as exc:
        return SourceResult(
            source, str(manual_csv.MANUAL_DIR), f"ERROR: {type(exc).__name__}", "-", "-",
            note=str(exc)[:250],
        )

    return SourceResult(
        source=source,
        url=str(result.file.path),
        status="OK",
        rows=str(result.row_count),
        nonzero="-",
        note=result.summary,
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
        run_espn_projections(season),
        run_ffc(teams=12, year=season),
    ]
    results.extend(run_manual_csv(pos, season) for pos in manual_csv.POSITIONS)
    _print_table(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
