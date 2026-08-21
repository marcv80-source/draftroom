"""Team-envelope report, per source: do the projected slices add up to a plausible team pie?

Runs :mod:`draftroom.valuation.envelope` over the real cached 2026 projections from all three
independent source families (Sleeper, ESPN, FantasyPros) PLUS the equal-weight blend that
is the default board, and prints:

  1. the fitted bands, with exactly what each was fitted on and what could not be fitted;
  2. per source, the passing-vs-receiving accounting identity for all 32 teams;
  3. per source, every team-stat sitting outside its fitted band, and the players driving it;
  4. the (source, stat) pairs this evidence would put in front of a human.

Reads only cached files under ``data/raw/`` -- no network call. Never run
``prep/fetch_all.py`` to "refresh" for this: CLAUDE.md documents that it writes new timestamped
raw files, moves what ``load_latest_raw()`` resolves to, and breaks unrelated tests.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\check_envelopes.py
    ... --teams-detail          also dump the full per-team sum table for every source
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk  # noqa: E402
from draftroom.prep.ffc_client import parse_adp_rows  # noqa: E402
from draftroom.prep.http import load_latest_raw  # noqa: E402
from draftroom.prep.sleeper_client import (  # noqa: E402
    filter_active_skill_players,
    to_statlines,
)
from draftroom.valuation import envelope as env  # noqa: E402

SEASON = 2026
#: The one season of team-season ACTUALS available offline (see envelope.team_season_actuals).
FIT_SEASON = 2025


def load_sources() -> tuple[dict[str, dict], dict[str, str], dict[str, str], dict[str, int]]:
    """``(statlines per source, pid->team, pid->name, per-source rows dropped by the crosswalk)``.

    The crosswalk and the two resolvers are imported from ``draftroom.validate.board``, not
    re-derived here: that module already owns the ESPN/FantasyPros -> pid join, and a second
    copy of it would be a second thing to keep correct.
    """
    from draftroom.validate.board import (
        _resolve_espn_statlines,
        _resolve_fantasypros_statlines,
    )
    from draftroom.prep import espn_client, manual_csv

    sleeper_raw = load_latest_raw("sleeper")
    ffc_rows = parse_adp_rows(load_latest_raw("ffc"))
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)

    universe = filter_active_skill_players(sleeper_raw)
    team_of = {pid: ref.team for pid, ref in universe.items()}
    name_of = {pid: ref.name for pid, ref in universe.items()}

    sleeper = {
        pid: line
        for pid, line in to_statlines(load_latest_raw("sleeper_projections")).items()
        if line.has_nonzero_stats()
    }
    espn = _resolve_espn_statlines(cw)
    fantasypros = _resolve_fantasypros_statlines(cw)

    # How many rows each source published vs. how many survived the crosswalk. An undershoot
    # is only interpretable if you know how much of the source went missing on the way in.
    dropped: dict[str, int] = {"sleeper": 0}
    try:
        espn_published = len(espn_client.to_statlines(load_latest_raw("espn")["players"], SEASON))
        dropped["espn"] = max(espn_published - len(espn), 0)
    except Exception:  # noqa: BLE001 - a missing cache is a normal state here
        dropped["espn"] = 0
    try:
        fp_published = sum(
            r.row_count for r in manual_csv.load_all_positions(season=SEASON).values()
        )
        dropped["fantasypros"] = max(fp_published - len(fantasypros), 0)
    except Exception:  # noqa: BLE001
        dropped["fantasypros"] = 0

    out = {"sleeper": sleeper, "espn": espn, "fantasypros": fantasypros}

    # The equal-weight blend is the DEFAULT board (docs/PLAN_2026-08-20.md, B1), so a check that
    # cannot see it cannot say anything about what actually ships. Best-effort: composite.py is
    # young, and a signature change there must degrade this report, never break it.
    try:
        from draftroom.valuation.composite import blend_statlines

        pids = set().union(*(set(d) for d in out.values()))
        out["blend"] = {
            pid: blend_statlines({key: lines.get(pid) for key, lines in out.items()})[0]
            for pid in pids
        }
        dropped["blend"] = 0
    except Exception as exc:  # noqa: BLE001
        print(f"  (no blend column: {type(exc).__name__}: {exc})")

    for source_lines in out.values():
        for pid in source_lines:
            name_of.setdefault(pid, pid)

    return out, team_of, name_of, dropped


def print_bands(bandset: env.BandSet) -> None:
    print("=" * 100)
    print(f"FITTED TEAM-SEASON BANDS  (fit season {FIT_SEASON})")
    print("=" * 100)
    prov = bandset.provenance
    print(f"  team-season observations : {prov['n_team_seasons']}  ({prov['team_actual_source']})")
    print(f"  drift seasons            : {prov['drift_seasons']}")
    print(f"  drift source             : {prov['drift_source']}")
    print(f"  drift measured directly  : {prov['drift_measured_stats']}")
    print(f"  drift proxy (transported): {prov['drift_proxy_low']:+.1%} / {prov['drift_proxy_high']:+.1%}")
    print(f"  LIMITS                   : {prov['limits']}")
    print()
    print(f"  {'stat':<10} {'obs min':>8} {'median':>8} {'obs max':>8} "
          f"{'band low':>9} {'band high':>9} {'drift':>16}  fitted?")
    for stat in env.BAND_STATS:
        band = bandset.bands.get(stat)
        if band is None:
            print(f"  {stat:<10} -- no band produced (stat absent from the fitted actuals)")
            continue
        print(
            f"  {stat:<10} {band.observed_min:>8.0f} {band.median:>8.0f} {band.observed_max:>8.0f} "
            f"{band.low:>9.0f} {band.high:>9.0f} "
            f"{band.drift_low:>+7.1%}/{band.drift_high:>+7.1%}  "
            f"{'measured' if band.drift_measured else 'PROXY (assumption)'}"
        )
    print()


def print_identity(report: env.EnvelopeReport, tolerances) -> None:
    print(f"-- accounting identities: {report.source} "
          f"({report.n_statlines} statlines, {report.n_no_team} with no team, "
          f"{report.n_dropped_unresolved} source rows lost in the crosswalk)")
    print("   tolerance per rule (largest deviation seen in the REAL 2025 actuals): "
          + ", ".join(f"{k}={v:.2%}" for k, v in sorted(tolerances.items())))
    rules = list(env.IDENTITY_RULES)
    print(f"   {'team':>5} " + " ".join(f"{r.split('_vs_')[1][:9]:>11}" for r in rules))
    by_team: dict[str, dict[str, env.IdentityCheck]] = {}
    for check in report.identity:
        by_team.setdefault(check.team, {})[check.rule] = check
    for team in sorted(by_team):
        cells = []
        for rule in rules:
            check = by_team[team].get(rule)
            if check is None:
                cells.append(f"{'--':>11}")
                continue
            mark = "!" if check.verdict == "overage" else ("." if check.verdict == "shortfall" else " ")
            cells.append(f"{check.delta_pct:>+9.1%}{mark} ")
        print(f"   {team:>5} " + " ".join(cells))
    viol = report.identity_violations
    print(f"   VIOLATIONS (receiving side above the passing side, '!'): {len(viol)} of "
          f"{len(report.identity)} checks")
    if viol:
        worst = sorted(viol, key=lambda c: -c.delta_pct)[:8]
        for c in worst:
            print(f"     {c.team:>4} {c.rule:<26} {c.pass_stat}={c.pass_side:>7.0f} "
                  f"{c.recv_stat}={c.recv_side:>7.0f}  {c.delta:+.0f} ({c.delta_pct:+.1%})")
    shortfalls = [c for c in report.identity if c.verdict == "shortfall"]
    print(f"   shortfalls ('.', INFORMATIONAL -- see COVERAGE_CAVEAT): {len(shortfalls)}")
    print_identity_confound(report)
    print()


def print_identity_confound(report: env.EnvelopeReport) -> None:
    """The one honest objection to the identity check, measured rather than waved away.

    If a source publishes only a team's starting quarterback and no backup, the passing side is
    short by the backup's share and the receiving side looks inflated for no good reason. So:
    group the teams by how many passers the source actually projects, and print the gap for
    each group. If the gap survives on teams with a full quarterback room, it is real."""
    import statistics

    rows = []
    for check in report.identity:
        if check.rule != "completions_vs_receptions" or check.pass_side <= 0:
            continue
        sums = report.team_sums[check.team]
        rows.append((sums.count("pass_cmp"), check.delta_pct, sums.get("pass_att")))
    if not rows:
        return
    by_n: dict[int, list[float]] = {}
    for n_passers, gap, _ in rows:
        by_n.setdefault(n_passers, []).append(gap)
    print("   confound check -- is the receiving 'overage' just a missing backup QB?")
    for n_passers in sorted(by_n):
        gaps = by_n[n_passers]
        print(f"     teams with {n_passers} projected passer(s): n={len(gaps):>2}  "
              f"mean rec-vs-cmp gap {statistics.fmean(gaps):+.2%}  "
              f"median {statistics.median(gaps):+.2%}")
    xs = [float(n) for n, _, _ in rows]
    ys = [g for _, g, _ in rows]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
    corr = num / den if den else float("nan")
    mean_att = statistics.fmean([a for _, _, a in rows])
    print(f"     corr(passers projected, gap) = {corr:+.3f}   "
          f"mean team pass_att = {mean_att:.0f}")
    print("     (a passing side that is genuinely INCOMPLETE shows up as team pass_att well "
          "below the fitted median; compare the coverage line below.)")


def print_bands_violations(report: env.EnvelopeReport) -> None:
    over = report.band_violations
    under = [v for v in report.band if v.direction == "under"]
    print(f"-- fitted-band check: {report.source}")
    print(f"   OVERAGES (violations): {len(over)}   undershoots (informational): {len(under)}")
    for v in over:
        drift = "measured" if v.band.drift_measured else "PROXY drift"
        print(f"     {v.team:>4} {v.stat:<9} {v.value:>7.0f} vs high {v.band.high:>7.0f} "
              f"({v.excess:+.0f}, {v.excess_pct:+.1%}) [{drift}]")
        names = ", ".join(f"{n} {val:.0f}" for _, n, val in v.top_contributors[:4])
        print(f"          top contributors: {names}")
    if under:
        worst = sorted(under, key=lambda v: v.excess_pct)[:6]
        print("   worst undershoots (NOT violations, listed only to show the coverage gap):")
        for v in worst:
            print(f"     {v.team:>4} {v.stat:<9} {v.value:>7.0f} vs low {v.band.low:>7.0f} "
                  f"({v.excess:+.0f})")
    print()


def print_no_drift_sensitivity(report: env.EnvelopeReport, bandset: env.BandSet) -> None:
    """What the band check would say WITHOUT the drift widening -- i.e. against the fitted
    season's raw observed maximum. For attempts/targets/TDs that widening is a transported
    proxy, not a measurement, so this is the price of the assumption stated out loud."""
    raw = env.check_bands(report.team_sums, bandset, include_under=False, use_drift=False)
    over = [v for v in raw if v.direction == "over"]
    print(f"   sensitivity -- same check against the raw {FIT_SEASON} observed max (no drift "
          f"widening): {len(over)} overages")
    for v in over:
        print(f"     {v.team:>4} {v.stat:<9} {v.value:>7.0f} vs observed max "
              f"{v.band.observed_max:>7.0f} ({v.excess:+.0f}, {v.excess_pct:+.1%})")
        names = ", ".join(f"{n} {val:.0f}" for _, n, val in v.top_contributors[:4])
        print(f"          top contributors: {names}")


def print_coverage(report: env.EnvelopeReport, bandset: env.BandSet) -> None:
    """Is the band check even applicable to this source? A team sum near the fitted median
    means the source's population is close to a whole offense; far below means the band cannot
    bite, whatever it says."""
    print(f"   coverage vs the fitted {FIT_SEASON} median, across the 32 teams "
          "(median team ratio; ~1.0 means a near-complete offense):")
    cells = []
    for stat in env.BAND_STATS:
        band = bandset.bands.get(stat)
        if band is None or band.median <= 0:
            continue
        ratios = sorted(
            sums.get(stat) / band.median
            for team, sums in report.team_sums.items()
            if team and sums.get(stat) > 0
        )
        if not ratios:
            cells.append(f"{stat}=n/a")
            continue
        mid = ratios[len(ratios) // 2]
        cells.append(f"{stat}={mid:.2f}")
    print("     " + "  ".join(cells))


def print_team_table(report: env.EnvelopeReport) -> None:
    stats = ("pass_att", "pass_cmp", "pass_yd", "rush_att", "rush_yd", "rec", "rec_tgt",
             "rec_yd", "total_td")
    print(f"-- per-team sums: {report.source}")
    print(f"   {'tm':>4} {'n':>3} " + " ".join(f"{s:>8}" for s in stats))
    for team in sorted(report.team_sums):
        sums = report.team_sums[team]
        label = team or "(none)"
        print(f"   {label:>4} {sums.n_players:>3} " + " ".join(f"{sums.get(s):>8.0f}" for s in stats))
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teams-detail", action="store_true", help="dump full per-team sums")
    args = parser.parse_args(argv)

    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)
    logging.getLogger("draftroom.prep.sleeper").setLevel(logging.ERROR)
    logging.getLogger("draftroom.prep.espn").setLevel(logging.ERROR)

    espn_raw = load_latest_raw("espn")
    actuals = env.team_season_actuals(espn_raw, FIT_SEASON)
    weekly_path, weekly_rows = env.load_weekly_history_rows()
    yardage = env.league_yardage_means(weekly_rows)

    print()
    print(f"weekly history file: {weekly_path.name}  ({len(weekly_rows)} game rows, "
          f"seasons {min(yardage)}-{max(yardage)})")
    print(f"team-season actuals: {len(actuals)} teams from the cached ESPN payload, {FIT_SEASON}")
    print()
    print("cross-source sanity on the fit itself (two unrelated providers, same season):")
    for stat in ("pass_yd", "rush_yd", "rec_yd"):
        espn_mean = sum(a.get(stat, 0.0) for a in actuals.values()) / max(len(actuals), 1)
        nfl_mean = yardage[FIT_SEASON][stat]
        print(f"   {stat:<8} ESPN team mean {espn_mean:>7.0f}   nflreadpy league/32 {nfl_mean:>7.0f}"
              f"   delta {100 * (espn_mean - nfl_mean) / nfl_mean:>+5.1f}%")
    print()

    bandset = env.fit_bands(
        team_actuals=actuals, yardage_means=yardage, fit_season=FIT_SEASON
    )
    tolerances = env.fit_identity_tolerances(actuals)
    print_bands(bandset)

    print("=" * 100)
    print(f"THE 2026 PROJECTIONS, PER SOURCE")
    print("=" * 100)
    print(env.COVERAGE_CAVEAT)
    print()

    sources, team_of, name_of, dropped = load_sources()
    reports = []
    for source, statlines in sources.items():
        report = env.build_report(
            source, statlines, team_of, bandset, tolerances,
            name_of=name_of, n_dropped_unresolved=dropped.get(source, 0),
        )
        reports.append(report)
        print_identity(report, tolerances)
        print_bands_violations(report)
        print_no_drift_sensitivity(report, bandset)
        print_coverage(report, bandset)
        print()
        if args.teams_detail:
            print_team_table(report)

    print("=" * 100)
    print("REJECTION CANDIDATES  (evidence for a human, NOT wired into the composite)")
    print("=" * 100)
    candidates = env.rejection_candidates(reports)
    if not candidates:
        print("  none")
    for source, stat, reason in candidates:
        print(f"  {source:<12} {stat:<10} {reason}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
