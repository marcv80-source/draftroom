"""TD-regression report: which projected touchdown counts are outliers against their own yardage?

Fits :mod:`draftroom.valuation.td_regression` on the cached 2025 actuals, prints the fit itself
(predictor chosen, slope, R2, residual spread, dispersion, sample size, usage floor, |z| tail),
then applies it to the real cached 2026 projections from all three independent source families
for all three plus the equal-weight blend, and lists what trips the fitted threshold -- highlighting the flags that land on a player who is
actually in the ADP pool, because those are the only ones that can move a draft.

Reads only cached files under ``data/raw/`` -- no network call. Never run ``prep/fetch_all.py``
to "refresh" for this: CLAUDE.md documents that it writes new timestamped raw files, moves what
``load_latest_raw()`` resolves to, and breaks unrelated tests.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\check_td_regression.py
    ... --quantile 0.99        flag only against the fitted 99th-percentile tail
    ... --all                  list every flag, not just the ones in the ADP pool
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.prep import espn_client  # noqa: E402
from draftroom.prep.ffc_client import parse_adp_rows  # noqa: E402
from draftroom.prep.http import load_latest_raw  # noqa: E402
from draftroom.prep.sleeper_client import filter_active_skill_players  # noqa: E402
from draftroom.valuation import td_regression as tdr  # noqa: E402

FIT_SEASON = 2025


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quantile", type=float, default=0.95,
                        help="which fitted |z| quantile is the flag threshold (default 0.95)")
    parser.add_argument("--all", action="store_true",
                        help="list every flag, not just players in the ADP pool")
    args = parser.parse_args(argv)

    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)
    logging.getLogger("draftroom.prep.sleeper").setLevel(logging.ERROR)
    logging.getLogger("draftroom.prep.espn").setLevel(logging.ERROR)

    espn_raw = load_latest_raw("espn")
    actuals = tdr.player_season_actuals(espn_raw, FIT_SEASON)
    modelset = tdr.fit_td_models(actuals, seasons=(FIT_SEASON,))

    print()
    print("=" * 104)
    print(f"FITTED TD MODELS  (through-origin td = slope x predictor, season {FIT_SEASON})")
    print("=" * 104)
    prov = modelset.provenance
    print(f"  player-seasons available : {prov['n_player_seasons_available']}  ({prov['source']})")
    print(f"  seasons                 : {prov['seasons']}   min rows per fit: {prov['min_fit_rows']}")
    print(f"  groups with no model    : {prov['skipped'] or '(none)'}")
    print(f"  LIMITS                  : {prov['limits']}")
    print()
    print(f"  {'group':<12} {'predictor':<9} {'n':>4} {'slope/100':>10} {'R2':>6} "
          f"{'resid sd':>9} {'disp':>6} {'floor':>7}  {'|z| p90':>8} {'p95':>6} {'p99':>6}")
    for (pos, td_stat), model in sorted(modelset.models.items()):
        zq = model.z_quantiles
        print(f"  {pos + '/' + td_stat:<12} {model.predictor:<9} {model.n:>4} "
              f"{model.slope * 100:>10.3f} {model.r2:>6.3f} {model.resid_sd:>9.2f} "
              f"{model.dispersion:>6.2f} {model.usage_floor:>7.0f}  "
              f"{zq.get(0.90, float('nan')):>8.2f} {zq.get(0.95, float('nan')):>6.2f} "
              f"{zq.get(0.99, float('nan')):>6.2f}")
    print()
    print("  predictor choice is by R2 -- every candidate that met the row minimum, for the record:")
    for (pos, td_stat), model in sorted(modelset.models.items()):
        tried = ", ".join(f"{k}={v:.3f}" for k, v in sorted(model.candidate_r2.items()))
        print(f"    {pos + '/' + td_stat:<12} {tried}")
    print()
    print(f"  CAVEAT: {modelset.caveat}")
    print()

    # -- apply to the 2026 projections -------------------------------------
    from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk
    from draftroom.prep.sleeper_client import to_statlines
    from draftroom.validate.board import (
        _resolve_espn_statlines,
        _resolve_fantasypros_statlines,
    )

    sleeper_raw = load_latest_raw("sleeper")
    ffc_rows = parse_adp_rows(load_latest_raw("ffc"))
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    universe = filter_active_skill_players(sleeper_raw)
    pos_of = {pid: ref.pos for pid, ref in universe.items()}
    name_of = {pid: ref.name for pid, ref in universe.items()}

    adp_of: dict[str, float] = {}
    for row in ffc_rows:
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        pid = cw.resolve("ffc", key)
        if pid is not None and row.adp is not None:
            adp_of[str(pid)] = float(row.adp)

    sources = {
        "sleeper": {
            pid: line
            for pid, line in to_statlines(load_latest_raw("sleeper_projections")).items()
            if line.has_nonzero_stats()
        },
        "espn": _resolve_espn_statlines(cw),
        "fantasypros": _resolve_fantasypros_statlines(cw),
    }

    # The equal-weight blend is the DEFAULT board (docs/archive/PLAN_2026-08-20.md, B1), so it has to be
    # in the table. Best-effort: composite.py is young, and a signature change there must
    # degrade this report rather than break it.
    try:
        from draftroom.valuation.composite import blend_statlines

        pids = set().union(*(set(d) for d in sources.values()))
        sources["blend"] = {
            pid: blend_statlines({k: lines.get(pid) for k, lines in sources.items()})[0]
            for pid in pids
        }
    except Exception as exc:  # noqa: BLE001
        print(f"  (no blend column: {type(exc).__name__}: {exc})")

    print("=" * 104)
    print("AGGREGATE TD LEVEL PER SOURCE  (the same fit, summed instead of per-player)")
    print("=" * 104)
    print("  A per-player z-score is asking a question an R2 of ~0.5 can barely answer. Summed")
    print("  over a whole position group the noise cancels and what is left is the source's own")
    print("  touchdown RATE against the rate the league actually produced. ratio 1.00 == the")
    print("  fitted 2025 rate exactly.")
    print()
    print(f"  {'source':<12} {'group':<12} {'n':>4} {'projected':>10} {'expected':>9} "
          f"{'ratio':>7} {'agg z':>7}")
    for source, statlines in sources.items():
        for bias in tdr.source_bias(source, statlines, pos_of, modelset):
            print(f"  {bias.source:<12} {bias.pos + '/' + bias.td_stat:<12} {bias.n_players:>4} "
                  f"{bias.projected_total:>10.1f} {bias.expected_total:>9.1f} "
                  f"{bias.ratio:>7.3f} {bias.z:>+7.2f}")
    print()

    print("=" * 104)
    print(f"CALIBRATION BACKTEST -- ESPN ONLY  (its {FIT_SEASON} projection vs the {FIT_SEASON} it got)")
    print("=" * 104)
    print("  The one place a projected TD rate can be scored against the rate that actually")
    print("  happened: the cached ESPN payload holds both, on the same player ids. Nothing cached")
    print("  under data/raw/sleeper_projections/ is 2025 and the FantasyPros CSVs are 2026, so a")
    print("  finding here is about ESPN and says nothing comparative. Sleeper's 2025 projections")
    print("  ARE retrievable though (see docs/archive/SOURCE_BACKTEST.md) -- cache them and this table")
    print("  covers two families instead of one.")
    print("  Rates, not totals: projected totals overshoot every year because projections do not")
    print("  know who gets hurt, so total-vs-total measures availability, not TD calibration.")
    print()
    espn_2025 = espn_client.to_statlines(espn_raw["players"], FIT_SEASON)
    print(f"  {'group':<12} {'pred':<9} {'n':>4} {'proj rate':>10} {'act rate':>9} "
          f"{'ratio':>7}   (rate = TD per unit of predictor)")
    for cal in tdr.backtest_rate_calibration(
        "espn", espn_2025, actuals, modelset, season=FIT_SEASON
    ):
        print(f"  {cal.pos + '/' + cal.td_stat:<12} {cal.predictor:<9} {cal.n_players:>4} "
              f"{cal.projected_rate * 100:>10.3f} {cal.actual_rate * 100:>9.3f} "
              f"{cal.rate_ratio:>7.3f}")
    print()

    print("=" * 104)
    print(f"FLAGS ON THE 2026 PROJECTIONS  (threshold = fitted |z| p{args.quantile:.2f})")
    print("=" * 104)
    for source, statlines in sources.items():
        flags = tdr.flag_statlines(
            statlines, pos_of, modelset, name_of=name_of, quantile=args.quantile
        )
        ranked = [f for f in flags if f.player_id in adp_of]
        print(f"-- {source}: {len(flags)} flags over {len(statlines)} statlines "
              f"({len(ranked)} of them inside the ADP pool)")
        shown = flags if args.all else ranked
        if not shown:
            print("     (none)")
        for f in shown:
            adp = adp_of.get(f.player_id)
            adp_txt = f"adp {adp:5.1f}" if adp is not None else "unranked"
            print(f"     {f.name:<24} {f.pos:<3} {adp_txt:<10} {f.td_stat:<8} "
                  f"proj {f.projected_td:>5.1f} vs expected {f.expected_td:>5.1f} "
                  f"({f.delta:+5.1f}, z {f.z:+5.2f} vs {f.threshold:.2f})  "
                  f"{f.predictor}={f.predictor_value:.0f}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
