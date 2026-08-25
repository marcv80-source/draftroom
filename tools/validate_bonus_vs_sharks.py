"""Validate ``valuation/bonuses.py`` against FantasySharks' own threshold-clearing game counts.

WHY THIS EXISTS. ``valuation/bonuses.py`` estimates, from empirical hit-rate curves fitted on
the cached weekly history, how often a player clears each of this league's per-game yardage bonus thresholds. That
model has had **no external reference of any kind** since it was written: the 2025 backtest
compares it to what players ACTUALLY did (``actual_bonus``), which validates the fit against
history, but nothing has ever compared its FORECAST for 2026 to another forecaster's. Marc's
second reason for adding FantasySharks (docs/archive/PLAN_2026-08-20.md) is that it publishes exactly
that quantity: a projected count of GAMES in which a player clears each yardage threshold.

THIS IS A REPORT, NOT A BOARD CHANGE. Nothing here writes to the board, the curves, or the
snapshot, and the bonus model must not be adjusted on the strength of it. Two independent
forecasts of the same quantity disagreeing tells you the quantity is uncertain; it does not tell
you which one is right. Per docs/archive/PLAN_2026-08-20.md's durable rule ("every proposed correction
must beat a dumb null of equal magnitude before it ships"), a remedy would need its own
measured case against a null, on data that does not exist yet -- nobody has 2026 outcomes.

WHAT IS AND IS NOT COMPARABLE, and why the gaps are printed rather than filled
-----------------------------------------------------------------------------
Two separate coverage limits meet here, and both have to be respected or the report invents a
reference that does not exist:

1. **What FantasySharks publishes** (docs/FANTASYSHARKS.md, measured): passing thresholds cover
   250/300/350 yards, rushing 50/100, receiving 50/100/150/200 -- and the RECEIVING columns stop
   at 100 on the RB table. So against this league's schedule (pass 300/400/500, rush 100/150/200,
   rec 100/150/200) the +3 tier is covered for passing and rushing, and all three receiving
   tiers are covered for WR and TE only.
2. **What our own curves are fitted at.** ``load_curves()`` fits each (stat, position) curve at
   exactly this league's three tiers, and ``bonuses._hit_rate`` returns 0.0 -- a documented
   conservative default -- for any other threshold. So FantasySharks' extra 250/350-passing and
   50-yard columns, which docs/FANTASYSHARKS.md rightly notes constrain the shape of the same
   distribution, have **no model counterpart to compare against**. Fitting new curves at those
   thresholds would be a change to the bonus model, which this tool is explicitly not allowed to
   make.

Every cell of the cross-product is therefore printed with one of three verdicts: COMPARED, NO
SOURCE REFERENCE (FantasySharks publishes no such column), or NO MODEL CURVE (our curves are not
fitted at that threshold). Nothing is interpolated.

THE ONE ASSUMPTION, STATED BECAUSE IT IS UNAVOIDABLE
----------------------------------------------------
FantasySharks publishes NO games column (re-measured every run by its own ``games_report()``: 0
games-shaped headers on all four tables, 0 distinct positive values across 516 players). Its
threshold counts are therefore counts over an unstated number of games, and its season yardage is
an unstated-length season. This tool reads both as a full season and uses the LEAGUE's own season
length (``LeagueConfig.weeks``) as the divisor -- which is not a choice made here, it is exactly
what ``validate/board.py::_games_divisor`` already does with a FantasySharks stat line, so the
model being validated is the model as the board actually uses it. Note the DIRECTION of the risk
this carries: if FantasySharks' internal season were 16 rather than 17 games, our yards-per-game
would be understated by ~6%, and because these are tail probabilities that lowers our predicted
counts by considerably more than 6%. Part of any deficit below could therefore be this artifact
rather than a real disagreement, which is why the report also prints a HISTORICAL referee. The
largest count FantasySharks publishes anywhere is printed too: it is the only available
upper-bound evidence on its internal season length.

Reads only cached files under ``data/raw/`` -- no network.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\validate_bonus_vs_sharks.py
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.prep import fantasysharks_client as fs  # noqa: E402
from draftroom.prep.crosswalk import (  # noqa: E402
    DYNASTYPROCESS_SOURCE,
    build_crosswalk,
)
from draftroom.prep.ffc_client import parse_adp_rows  # noqa: E402
from draftroom.prep.http import load_latest_raw  # noqa: E402
from draftroom.valuation.bonuses import (  # noqa: E402
    _hit_rate,
    load_bonus_schedule,
    load_curves,
)

POSITIONS = ("QB", "RB", "WR", "TE")


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None  # a constant column has no correlation -- never report 0.0 as if measured
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _historical_counts() -> dict[tuple[str, str, float], dict[str, int]]:
    """``(pos, stat, threshold) -> {season: player-games clearing it}`` from cached weekly data.

    The referee for the whole report. Reads ``data/raw/nflreadpy_weekly/*.csv`` -- the same
    cached weekly history the bonus curves were fitted on -- and counts, per season, how many
    real player-games at each position cleared each threshold. De-duplicated on
    (season, week, player_id) because more than one cached pull may cover overlapping seasons.
    """
    import csv
    import glob

    seen: set[tuple[str, str, str]] = set()
    out: dict[tuple[str, str, float], dict[str, int]] = {}
    tiers = {"pass_yd": (300.0,), "rush_yd": (100.0,), "rec_yd": (100.0, 150.0, 200.0)}
    root = Path(__file__).resolve().parents[1]
    for path in sorted(glob.glob(str(root / "data" / "raw" / "nflreadpy_weekly" / "*.csv"))):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = (row["season"], row["week"], row["player_id"])
                if key in seen:
                    continue
                seen.add(key)
                pos = (row.get("position") or "").strip().upper()
                if pos not in POSITIONS:
                    continue
                for stat, thrs in tiers.items():
                    try:
                        yd = float(row.get(stat) or 0.0)
                    except ValueError:
                        continue
                    for thr in thrs:
                        cell = out.setdefault((pos, stat, thr), {})
                        cell.setdefault(row["season"], 0)
                        if yd >= thr:
                            cell[row["season"]] += 1
    return out


#: The three verdicts a (position, stat, league tier) cell can carry. Nothing is interpolated:
#: a cell with no external number and a cell with no model number are distinct facts and are
#: reported as such.
COMPARED = "COMPARED"
NO_SOURCE = "NO SOURCE REFERENCE (FantasySharks publishes no such column)"
NO_CURVE = "NO MODEL CURVE (our curve is not fitted at this threshold)"


def coverage(schedule, curves) -> dict[tuple[str, str, float], str]:
    """Classify every (position, league bonus stat, league tier) cell.

    Factored out of :func:`main` so the coverage claims in this module's docstring -- passing and
    rushing cover only the +3 tier, receiving covers all three tiers but only for WR and TE --
    are asserted by a test rather than trusted.
    """
    published = {
        (pos, stat, thr) for pos, pairs in fs.THRESHOLDS_BY_POS.items() for stat, thr in pairs
    }
    out: dict[tuple[str, str, float], str] = {}
    for pos in POSITIONS:
        for stat, tiers in schedule.items():
            for tier in tiers:
                thr = float(tier["threshold"])
                curve = curves.get((stat, pos))
                has_curve = curve is not None and thr in curve.thresholds
                if (pos, stat, thr) not in published:
                    out[(pos, stat, thr)] = NO_SOURCE
                elif not has_curve:
                    out[(pos, stat, thr)] = NO_CURVE
                else:
                    out[(pos, stat, thr)] = COMPARED
    return out


def unpaid_thresholds(schedule) -> list[tuple[str, float]]:
    """The threshold counts FantasySharks publishes that this league does not pay a bonus for."""
    return sorted(
        {
            (stat, thr)
            for _pos, pairs in fs.THRESHOLDS_BY_POS.items()
            for stat, thr in pairs
            if thr not in {float(t["threshold"]) for t in schedule.get(stat, ())}
        }
    )


def _ranked_pids() -> tuple[set[str], object]:
    """Crosswalk pids for every skill player in the cached FFC ADP feed (the board's ranked
    tier). Used only to split the report -- the deep FantasySharks tail is real data but the
    board never values it, so a metric pooled over all 516 rows answers a different question
    from one pooled over the ~189 players Marc can actually draft off the board."""
    ffc_rows = parse_adp_rows(load_latest_raw("ffc"))
    sleeper_raw = load_latest_raw("sleeper")
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    out: set[str] = set()
    for row in ffc_rows:
        key = (
            str(row.player_id)
            if row.player_id is not None
            else f"{row.name}|{row.team}|{row.pos}"
        )
        pid = cw.resolve("ffc", key)
        if pid is not None:
            out.add(str(pid))
    return out, cw


def main() -> int:
    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)

    cfg = LeagueConfig.from_yaml()
    weeks = float(cfg.weeks)
    schedule = load_bonus_schedule()
    curves = load_curves()

    payload = fs.load_cached()
    pages = fs.pages_of(payload)
    rows = fs.parse_all(pages)
    games = fs.games_report(pages, rows)

    print("=" * 100)
    print("BONUS MODEL vs FANTASYSHARKS THRESHOLD COUNTS -- validation report, not a board change")
    print("=" * 100)
    print(
        f"league: {cfg.teams} teams, {cfg.weeks} weeks (data/league_manual.yaml). "
        f"FantasySharks payload: season {payload.get('season')}, segment "
        f"{payload.get('segment')} ({payload.get('segment_label')!r}), {len(rows)} players."
    )
    print(
        "FantasySharks games column: "
        f"{len(games['games_columns'])} games-shaped headers across the four tables, "
        f"{games['distinct_values']} distinct positive `games` values over "
        f"{games['players_parsed']} parsed players -> divisor falls back to LeagueConfig.weeks "
        f"= {cfg.weeks:g}."
    )
    biggest = max(
        ((c.games, r.name, c.stat, c.threshold) for r in rows for c in r.thresholds),
        default=(0.0, "-", "-", 0.0),
    )
    print(
        f"largest threshold count FantasySharks publishes anywhere: {biggest[0]:.1f} games "
        f"({biggest[1]}, {biggest[2]} >= {biggest[3]:.0f}) -- the only upper-bound evidence on "
        f"its internal season length; {cfg.weeks:g} weeks is this league's."
    )

    ranked, _cw = _ranked_pids()
    fs_pid: dict[str, str | None] = {}
    for r in rows:
        entry = _cw.resolve_fantasysharks_row(r.source_key, r.name, r.team, r.pos)
        fs_pid[r.source_key] = entry.pid
    n_ranked_rows = sum(1 for r in rows if fs_pid.get(r.source_key) in ranked)
    print(
        f"crosswalk: {sum(1 for v in fs_pid.values() if v)} of {len(rows)} FantasySharks rows "
        f"resolved; {n_ranked_rows} of them are in the ADP-ranked tier the board values."
    )

    # ---------------------------------------------------------------- coverage cross-product
    print("\n" + "-" * 100)
    print("COVERAGE: every (position, stat, league tier) cell, and why it is or is not comparable")
    print("-" * 100)
    print(f"{'pos':4s} {'stat':9s} {'tier':>6s}  verdict")
    cells = coverage(schedule, curves)
    comparable = [cell for cell, verdict in cells.items() if verdict == COMPARED]
    for (pos, stat, thr), verdict in cells.items():
        print(f"{pos:4s} {stat:9s} {thr:6.0f}  {verdict}")

    extra = unpaid_thresholds(schedule)
    print(
        "\nFantasySharks also publishes counts this league does not pay: "
        + ", ".join(f"{s} >= {t:.0f}" for s, t in extra)
        + ". They are NOT compared: bonuses.load_curves() fits each curve at this league's three "
        "tiers only and bonuses._hit_rate returns a conservative 0.0 elsewhere, so there is no "
        "model number to compare them to. Fitting new curves would be a change to the bonus "
        "model, which this tool does not make."
    )

    # -------------------------------------------------------------------- the comparison
    def compare(pool_rows, label: str) -> None:
        print("\n" + "-" * 100)
        print(f"COMPARISON -- {label} ({len(pool_rows)} FantasySharks rows)")
        print("-" * 100)
        print(
            f"{'pos':4s} {'stat':9s} {'tier':>5s} {'n':>4s} "
            f"{'theirs':>7s} {'ours':>7s} {'MAD':>6s} {'bias':>7s} {'bias%':>7s} {'corr':>7s} "
            f"{'ours>':>6s}"
        )
        per_pos: dict[str, list[tuple[float, float]]] = {}
        for pos, stat, thr in comparable:
            curve = curves[(stat, pos)]
            theirs_v: list[float] = []
            ours_v: list[float] = []
            detail: list[tuple[float, str, float, float]] = []
            both_zero = 0
            for r in pool_rows:
                if r.pos != pos:
                    continue
                theirs = r.thresholds and next(
                    (c.games for c in r.thresholds if c.stat == stat and c.threshold == thr),
                    None,
                )
                if theirs is None:
                    continue
                season_yd = float(getattr(r.stats, stat))
                if season_yd <= 0.0:
                    # The source projects no production in this stat at all. Its count is 0
                    # because there is nothing to clear, and our model's is 0 for the same
                    # reason: comparing those pairs measures agreement about a non-event and
                    # would inflate every statistic in this table.
                    both_zero += 1
                    continue
                ypg = season_yd / weeks
                ours = weeks * _hit_rate(curve, ypg, thr)
                theirs_v.append(float(theirs))
                ours_v.append(ours)
                detail.append((ours - float(theirs), r.name, float(theirs), ours))
                per_pos.setdefault(pos, []).append((float(theirs), ours))
            n = len(theirs_v)
            if n == 0:
                print(f"{pos:4s} {stat:9s} {thr:5.0f}    0  (no rows with projected yardage)")
                continue
            mt = sum(theirs_v) / n
            mo = sum(ours_v) / n
            mad = sum(abs(o - t) for o, t in zip(ours_v, theirs_v)) / n
            bias = mo - mt
            biaspct = (bias / mt * 100.0) if mt > 0 else float("nan")
            corr = _pearson(ours_v, theirs_v)
            over = sum(1 for o, t in zip(ours_v, theirs_v) if o > t)
            corr_s = f"{corr:7.3f}" if corr is not None else "      -"
            print(
                f"{pos:4s} {stat:9s} {thr:5.0f} {n:4d} "
                f"{mt:7.2f} {mo:7.2f} {mad:6.2f} {bias:+7.2f} {biaspct:+6.1f}% {corr_s} "
                f"{over:4d}/{n:<4d}"
                + (f"   [{both_zero} rows skipped: no projected {stat}]" if both_zero else "")
            )
            if label.startswith("ranked"):
                detail.sort(key=lambda d: -abs(d[0]))
                for d, name, t, o in detail[:3]:
                    print(f"      biggest gap: {name:22s} theirs {t:5.1f}  ours {o:5.1f}  ({d:+.1f})")

        print()
        for pos, pairs in per_pos.items():
            t = [a for a, _ in pairs]
            o = [b for _, b in pairs]
            corr = _pearson(o, t)
            corr_s = f"{corr:.3f}" if corr is not None else "n/a"
            print(
                f"  {pos} pooled over its comparable tiers: n={len(pairs)}, "
                f"mean theirs {sum(t)/len(t):.2f}, mean ours {sum(o)/len(o):.2f}, "
                f"MAD {sum(abs(x-y) for x, y in zip(o, t))/len(pairs):.2f}, corr {corr_s}"
            )

    compare(rows, "all FantasySharks rows")
    compare(
        [r for r in rows if fs_pid.get(r.source_key) in ranked],
        "ranked tier only (in the FFC ADP feed -- the players the board values)",
    )

    # ------------------------------------------------------- the arbiter: what actually happens
    #
    # Neither forecast is ground truth, but the QUANTITY both forecast -- how many
    # threshold-clearing games happen in an NFL season -- is a thing that has already happened
    # seven times in the cached weekly history. Summing each side over the whole position group
    # gives a league-wide total that history can adjudicate directly. This is the only part of
    # this report with an outside referee, and it is the reason it can say which side to trust.
    hist = _historical_counts()
    print("\n" + "-" * 100)
    print("REALITY CHECK: league-wide threshold-clearing GAMES per season, forecast vs history")
    print("-" * 100)
    print(
        f"{'pos':4s} {'stat':9s} {'tier':>5s} {'theirs':>8s} {'ours':>8s}  "
        f"{'history: mean':>13s} {'range':>13s}   seasons"
    )
    for pos, stat, thr in comparable:
        curve = curves[(stat, pos)]
        theirs_total = 0.0
        ours_total = 0.0
        for r in rows:
            if r.pos != pos:
                continue
            t = next(
                (c.games for c in r.thresholds if c.stat == stat and c.threshold == thr), None
            )
            if t is None:
                continue
            theirs_total += float(t)
            season_yd = float(getattr(r.stats, stat))
            if season_yd > 0:
                ours_total += weeks * _hit_rate(curve, season_yd / weeks, thr)
        per_season = hist.get((pos, stat, thr), {})
        if per_season:
            vals = [per_season[s] for s in sorted(per_season)]
            mean_h = sum(vals) / len(vals)
            rng = f"{min(vals)}-{max(vals)}"
            seasons = ",".join(sorted(per_season))
        else:
            mean_h, rng, seasons = float("nan"), "-", "-"
        print(
            f"{pos:4s} {stat:9s} {thr:5.0f} {theirs_total:8.1f} {ours_total:8.1f}  "
            f"{mean_h:13.1f} {rng:>13s}   {seasons}"
        )
    print(
        "\nHistory is every player-game in data/raw/nflreadpy_weekly/ at that position clearing "
        "that threshold, counted per season and then averaged -- real outcomes, not a model. It "
        "is not a like-for-like ceiling (history counts every player who took the field; "
        "FantasySharks lists 516) but the direction of a 2x or 5x gap is not ambiguous."
    )

    print("\n" + "=" * 100)
    print(
        "READ THIS TABLE AS A DISAGREEMENT MEASURE, NOT A SCORE. Neither side is ground truth: "
        "ours is a hit-rate curve fitted on the cached real weekly outcomes and then driven by "
        "FantasySharks' OWN season yardage (so the curve is what is being tested, not the "
        "projection); theirs is FantasySharks' unpublished internal method. A systematic bias "
        "here is a finding for Marc, and the bonus model is NOT to be adjusted on it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
