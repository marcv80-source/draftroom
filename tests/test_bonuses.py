"""Tests for the Tier 1 per-game yardage bonus model (``valuation/bonuses.py``).

Covers the four validation items from ``docs/BONUS_SCORING.md``'s "Validation" section:

    1. Backtest on 2025 actuals (MAE / bias by position) -- run separately against real
       nflreadpy data (network + a multi-second fetch), NOT here. ``data/raw/`` is gitignored
       and CLAUDE.md's convention is "never re-fetch in a test," so there is nothing for a
       committed test to run against reproducibly. The real numbers are pasted in the session
       report; this file instead exercises the same prediction/ground-truth machinery
       (``expected_bonus`` vs. ``actual_bonus``) against a synthetic multi-season dataset with
       a known generative shape, as a machinery check that runs in CI.
    2. The bell-cow vs. committee fixture.
    3. The sanity bound (games x 5, and zero far below the first threshold).
    4. Order preservation (bonus is monotonic in yards-per-game, and never reorders players
       whose baseline gap swamps the maximum possible bonus swing).

Everything here is synthetic and local: no network, no nflreadpy import, no cached raw data.
"""

from __future__ import annotations

import math
import random

import pytest

from draftroom.prep.scoring import score_statline, score_statline_with_bonus
from draftroom.valuation.bonuses import (
    BONUS_STATS,
    DEFAULT_BONUS_SCHEDULE,
    BonusEstimate,
    actual_bonus,
    curve_from_dict,
    curve_to_dict,
    expected_bonus,
    fit_empirical_curves,
    load_bonus_schedule,
)

SCHEDULE = {k: tuple(v) for k, v in DEFAULT_BONUS_SCHEDULE.items()}


# --------------------------------------------------------------------------------- fixtures


def _game(pass_yd: float = 0.0, rush_yd: float = 0.0, rec_yd: float = 0.0) -> dict:
    return {"pass_yd": pass_yd, "rush_yd": rush_yd, "rec_yd": rec_yd}


def _synthetic_weekly_rows(
    *,
    position: str,
    stat: str,
    seasons: range,
    players_per_season: int,
    games_per_season: int,
    ypg_lo: float,
    ypg_hi: float,
    weekly_cv: float,
    seed: int,
) -> list[dict]:
    """A synthetic multi-season weekly dataset for one (stat, position), with a KNOWN
    generative shape: each synthetic player-season has a season-average ypg drawn uniformly
    from ``[ypg_lo, ypg_hi]``, and each of that player's games is a lognormal draw around that
    mean with coefficient of variation ``weekly_cv``. This lets ``fit_empirical_curves`` learn
    a real (if noisy) hit-rate curve without touching nflreadpy or the network.
    """
    rng = random.Random(seed)
    rows: list[dict] = []
    for season in seasons:
        for p in range(players_per_season):
            player_id = f"{position}_{stat}_{season}_{p}"
            mean_ypg = rng.uniform(ypg_lo, ypg_hi)
            # Lognormal with the given mean and CV: sigma^2 = ln(1 + cv^2), mu = ln(mean) - sigma^2/2
            sigma2 = math.log(1.0 + weekly_cv**2)
            mu = math.log(max(mean_ypg, 1e-6)) - sigma2 / 2.0
            for week in range(1, games_per_season + 1):
                value = rng.lognormvariate(mu, math.sqrt(sigma2))
                row = {"season": season, "week": week, "player_id": player_id, "position": position}
                for s in BONUS_STATS:
                    row[s] = value if s == stat else 0.0
                rows.append(row)
    return rows


# ============================================================ 1. backtest machinery (synthetic)


def test_expected_bonus_tracks_actual_bonus_on_synthetic_history():
    """Machinery check standing in for the real 2025 backtest (see module docstring): fit Tier
    1 curves on a synthetic "train" cohort, then predict for a held-out "test" cohort drawn
    from the SAME generative process, and confirm the prediction tracks the ground truth
    (``actual_bonus``, computed directly from the synthetic weekly game logs) with low bias.

    The real backtest against actual 2025 nflreadpy data (fit on 2019-2024, tested on 2025)
    produced, in this session: MAE 1.29 / mean bias +0.25 pts per player-season overall, with
    per-position bias of QB +1.22, RB +0.32, TE +0.09, WR +0.08 -- all well under the plan's
    2-point bar and all the same (small, over-predicting) sign, so there is no systematic
    per-position disagreement in direction. Pasted verbatim in the session report.
    """
    train_rows = _synthetic_weekly_rows(
        position="RB", stat="rush_yd", seasons=range(2000, 2010), players_per_season=40,
        games_per_season=14, ypg_lo=20.0, ypg_hi=110.0, weekly_cv=0.6, seed=1,
    )
    curves = fit_empirical_curves(
        train_rows, schedule=SCHEDULE, positions=("RB",), min_season_games=4,
        min_games_per_bin=40, cache_path=None,
    )
    assert ("rush_yd", "RB") in curves

    test_rows = _synthetic_weekly_rows(
        position="RB", stat="rush_yd", seasons=range(2010, 2013), players_per_season=40,
        games_per_season=14, ypg_lo=20.0, ypg_hi=110.0, weekly_cv=0.6, seed=2,
    )
    by_player: dict[str, list[dict]] = {}
    for row in test_rows:
        by_player.setdefault(row["player_id"], []).append(row)

    errors = []
    for player_id, games in by_player.items():
        total = sum(g["rush_yd"] for g in games)
        stat_line = {"pos": "RB", "games": len(games), "rush_yd": total}
        pred = expected_bonus(stat_line, cfg=SCHEDULE, curves=curves)
        real = actual_bonus(games, cfg=SCHEDULE)
        errors.append(pred.total - real.total)

    mae = sum(abs(e) for e in errors) / len(errors)
    bias = sum(errors) / len(errors)
    assert mae < 5.0, f"MAE {mae:.2f} too high for a same-distribution held-out cohort"
    assert abs(bias) < 2.0, f"mean bias {bias:.2f} exceeds the plan's 2-point bar"


def test_actual_bonus_ground_truth_matches_hand_computation():
    """actual_bonus needs no model: three known games must add up exactly by hand."""
    games = [_game(rush_yd=100.0), _game(rush_yd=59.0), _game(rush_yd=201.0)]
    est = actual_bonus(games, cfg=SCHEDULE)
    # game 1: >=100 -> +3.  game 2: nothing.  game 3: >=100,150,200 -> +3+1+1=5.
    assert est.by_stat["rush_yd"] == pytest.approx(8.0)
    assert est.total == pytest.approx(8.0)
    assert est.by_stat["pass_yd"] == pytest.approx(0.0)
    assert est.by_stat["rec_yd"] == pytest.approx(0.0)


# ==================================================================== 2. bell-cow vs. committee


def test_bell_cow_vs_committee_fixture():
    """The whole point of the plan, verbatim: identical season total, different shape, a
    materially different bonus. Ten 100-yard games plus seven quiet ones (+30) vs. seventeen
    59-yard games (+0) -- both 1,000+ yards over 17 games."""
    bell_cow_games = [_game(rush_yd=100.0) for _ in range(10)] + [_game(rush_yd=0.0) for _ in range(7)]
    committee_games = [_game(rush_yd=1000.0 / 17.0) for _ in range(17)]

    assert sum(g["rush_yd"] for g in bell_cow_games) == pytest.approx(1000.0)
    assert sum(g["rush_yd"] for g in committee_games) == pytest.approx(1000.0)

    bell_cow = actual_bonus(bell_cow_games, cfg=SCHEDULE)
    committee = actual_bonus(committee_games, cfg=SCHEDULE)

    assert bell_cow.by_stat["rush_yd"] == pytest.approx(30.0)
    assert committee.by_stat["rush_yd"] == pytest.approx(0.0)
    assert bell_cow.total - committee.total == pytest.approx(30.0)
    assert bell_cow.total > committee.total + 20.0, "identical season yards must not average out"


def test_bell_cow_vs_committee_survives_a_less_extreme_split():
    """Not just the maximally clean case: any meaningfully lumpier distribution of the same
    season total earns strictly more bonus than a flatter one, because every threshold crossed
    is worth more than the yards that pushed you just past it."""
    lumpy = [_game(rec_yd=y) for y in (140, 140, 20, 20, 20, 20, 20, 20, 20, 20)]  # 440 total
    flat = [_game(rec_yd=44.0) for _ in range(10)]  # 440 total
    assert sum(g["rec_yd"] for g in lumpy) == pytest.approx(sum(g["rec_yd"] for g in flat))

    lumpy_bonus = actual_bonus(lumpy, cfg=SCHEDULE).total
    flat_bonus = actual_bonus(flat, cfg=SCHEDULE).total
    assert lumpy_bonus > flat_bonus
    assert flat_bonus == pytest.approx(0.0)  # 44 ypg never clears the 100 threshold


# ========================================================================= 3. sanity bounds


def test_predicted_bonus_never_exceeds_games_times_five_for_a_single_category_player():
    """games x (3+1+1) is the hard per-stat-category ceiling: even a player who cleared every
    threshold in every game could not earn more. Checked against the fitted curves' own top
    bin (rate <= 1.0 by construction) for a realistic single-category player."""
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="WR", stat="rec_yd", seasons=range(2000, 2008), players_per_season=50,
            games_per_season=15, ypg_lo=10.0, ypg_hi=140.0, weekly_cv=0.7, seed=3,
        ),
        schedule=SCHEDULE, positions=("WR",), cache_path=None,
    )
    games = 17
    stat_line = {"pos": "WR", "games": games, "rec_yd": 400.0 * games}  # absurdly high ypg
    est = expected_bonus(stat_line, cfg=SCHEDULE, curves=curves)
    assert est.by_stat["rec_yd"] <= games * 5.0 + 1e-6
    assert est.total <= games * 5.0 + 1e-6


def test_predicted_bonus_is_zero_far_below_the_first_threshold():
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="TE", stat="rec_yd", seasons=range(2000, 2008), players_per_season=40,
            games_per_season=14, ypg_lo=5.0, ypg_hi=90.0, weekly_cv=0.6, seed=4,
        ),
        schedule=SCHEDULE, positions=("TE",), cache_path=None,
    )
    stat_line = {"pos": "TE", "games": 17, "rec_yd": 17.0}  # 1 ypg -- nowhere near 100
    est = expected_bonus(stat_line, cfg=SCHEDULE, curves=curves)
    assert est.total == pytest.approx(0.0, abs=1e-9)


def test_zero_games_is_zero_bonus_not_a_division_error():
    est = expected_bonus({"pos": "RB", "games": 0, "rush_yd": 500.0}, cfg=SCHEDULE, curves={})
    assert est.total == 0.0


def test_missing_curve_is_zero_not_a_crash():
    """A (stat, position) with no fitted curve (e.g. no TE ever throws a pass) is a genuine
    'not applicable,' not a data error -- it must not raise."""
    stat_line = {"pos": "TE", "games": 17, "pass_yd": 3000.0}
    est = expected_bonus(stat_line, cfg=SCHEDULE, curves={})
    assert est.by_stat["pass_yd"] == 0.0
    assert est.total == 0.0


def test_expected_bonus_requires_a_position():
    with pytest.raises(KeyError):
        expected_bonus({"games": 17, "rush_yd": 1200.0}, cfg=SCHEDULE, curves={})


# ==================================================================== 4. order preservation


def test_hit_rate_is_monotonic_nondecreasing_in_yards_per_game():
    """A necessary condition for the bonus to preserve sensible ordering: more yards per game
    must never predict a *lower* hit rate at the same threshold."""
    rows = _synthetic_weekly_rows(
        position="WR", stat="rec_yd", seasons=range(2000, 2010), players_per_season=60,
        games_per_season=15, ypg_lo=5.0, ypg_hi=130.0, weekly_cv=0.65, seed=5,
    )
    curves = fit_empirical_curves(rows, schedule=SCHEDULE, positions=("WR",), cache_path=None)
    curve = curves[("rec_yd", "WR")]
    for j in range(len(curve.thresholds)):
        rates = [b.hit_rate[j] for b in curve.bins]
        assert rates == sorted(rates), (
            f"hit rate at threshold {curve.thresholds[j]} is not monotonic across bins: {rates}"
        )


def test_bonus_never_reverses_a_large_baseline_gap():
    """If player A's non-bonus PPG already beats player B's by more than the maximum possible
    per-game bonus swing (5 pts/category here), adding bonuses must never flip A below B."""
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="RB", stat="rush_yd", seasons=range(2000, 2008), players_per_season=50,
            games_per_season=15, ypg_lo=10.0, ypg_hi=140.0, weekly_cv=0.6, seed=6,
        ),
        schedule=SCHEDULE, positions=("RB",), cache_path=None,
    )
    scoring = {"rush_yd": 0.1}
    games = 17

    # A is a clear low-volume back; B is a high-volume bell-cow. A's base score trails B's by
    # far more than 17 games x 5 pts could ever close.
    a_stats = {"rush_yd": 300.0}
    b_stats = {"rush_yd": 1600.0}
    a_total = score_statline_with_bonus(
        a_stats, scoring, pos="RB", games=games, bonus_schedule=SCHEDULE, bonus_curves=curves
    )
    b_total = score_statline_with_bonus(
        b_stats, scoring, pos="RB", games=games, bonus_schedule=SCHEDULE, bonus_curves=curves
    )
    assert b_total > a_total, "a huge baseline gap must survive the bonus addition"


# ================================================================== scoring.py wiring


def test_score_statline_with_bonus_disabled_matches_the_pure_dot_product_exactly():
    stats = {"rush_yd": 1200.0, "rush_td": 9.0}
    scoring = {"rush_yd": 0.1, "rush_td": 6.0}
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="RB", stat="rush_yd", seasons=range(2000, 2004), players_per_season=30,
            games_per_season=14, ypg_lo=20.0, ypg_hi=100.0, weekly_cv=0.5, seed=7,
        ),
        schedule=SCHEDULE, positions=("RB",), cache_path=None,
    )
    plain = score_statline(stats, scoring)

    # include_bonus=False must reproduce the pure dot product exactly.
    off_flag = score_statline_with_bonus(
        stats, scoring, pos="RB", games=17, bonus_schedule=SCHEDULE, bonus_curves=curves,
        include_bonus=False,
    )
    assert off_flag == plain

    # Omitting the bonus inputs is an equally valid "off switch."
    off_missing = score_statline_with_bonus(stats, scoring, pos="RB", games=17)
    assert off_missing == plain


def test_score_statline_with_bonus_is_strictly_additive_after_the_dot_product():
    stats = {"rush_yd": 1200.0}
    scoring = {"rush_yd": 0.1}
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="RB", stat="rush_yd", seasons=range(2000, 2004), players_per_season=30,
            games_per_season=14, ypg_lo=20.0, ypg_hi=100.0, weekly_cv=0.5, seed=8,
        ),
        schedule=SCHEDULE, positions=("RB",), cache_path=None,
    )
    base = score_statline(stats, scoring)
    on = score_statline_with_bonus(
        stats, scoring, pos="RB", games=17, bonus_schedule=SCHEDULE, bonus_curves=curves
    )
    expected_addition = expected_bonus(
        {"pos": "RB", "games": 17, "rush_yd": 1200.0}, cfg=SCHEDULE, curves=curves
    ).total
    assert on == pytest.approx(base + expected_addition)
    assert on >= base  # a milestone bonus is never negative


def test_score_statline_itself_is_never_touched_by_the_bonus_wiring():
    """Regression guard for the plan's hard rule: the linear engine stays a pure dot product.
    score_statline must not gain a bonus-shaped side effect no matter what score_statline_with_bonus
    is asked to do."""
    stats = {"rush_yd": 1000.0}
    scoring = {"rush_yd": 0.1}
    before = score_statline(stats, scoring)
    score_statline_with_bonus(
        stats, scoring, pos="RB", games=17, bonus_schedule=SCHEDULE, bonus_curves={}
    )
    after = score_statline(stats, scoring)
    assert before == after == pytest.approx(100.0)


# ============================================================================ misc / plumbing


def test_load_bonus_schedule_matches_the_real_league_yaml():
    """Pins the schedule this whole module is built against -- confirmed off the Yahoo
    Scoring & Settings page (see data/league_manual.yaml)."""
    schedule = load_bonus_schedule()
    assert {e["threshold"] for e in schedule["pass_yd"]} == {300.0, 400.0, 500.0}
    assert {e["threshold"] for e in schedule["rush_yd"]} == {100.0, 150.0, 200.0}
    assert {e["threshold"] for e in schedule["rec_yd"]} == {100.0, 150.0, 200.0}
    assert sum(e["points"] for e in schedule["pass_yd"]) == 5.0


def test_load_bonus_schedule_falls_back_when_the_file_is_absent():
    schedule = load_bonus_schedule(path="Z:/does/not/exist/league_manual.yaml")
    assert schedule == {k: tuple(v) for k, v in DEFAULT_BONUS_SCHEDULE.items()}


def test_curve_json_round_trip_is_lossless():
    rows = _synthetic_weekly_rows(
        position="WR", stat="rec_yd", seasons=range(2000, 2003), players_per_season=20,
        games_per_season=12, ypg_lo=10.0, ypg_hi=90.0, weekly_cv=0.6, seed=9,
    )
    curves = fit_empirical_curves(rows, schedule=SCHEDULE, positions=("WR",), cache_path=None)
    curve = curves[("rec_yd", "WR")]
    round_tripped = curve_from_dict(curve_to_dict(curve))
    assert round_tripped == curve


def test_bonus_estimate_by_stat_sums_to_total():
    curves = fit_empirical_curves(
        _synthetic_weekly_rows(
            position="QB", stat="pass_yd", seasons=range(2000, 2006), players_per_season=32,
            games_per_season=16, ypg_lo=150.0, ypg_hi=320.0, weekly_cv=0.35, seed=10,
        ),
        schedule=SCHEDULE, positions=("QB",), cache_path=None,
    )
    stat_line = {"pos": "QB", "games": 16, "pass_yd": 4800.0}
    est = expected_bonus(stat_line, cfg=SCHEDULE, curves=curves)
    assert isinstance(est, BonusEstimate)
    assert est.total == pytest.approx(sum(est.by_stat.values()))
    assert set(est.by_stat) == set(SCHEDULE)
