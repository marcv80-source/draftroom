"""Tests for tools/check_renormalization.py -- the team-identity renormalization verdict.

The traps this file is aimed at, in order of how badly each would corrupt the answer:

1. **Reading a 2026 team for a 2025 projection.** ``docs/PROJECTION_CHALLENGES.md`` documents
   the trap and this tool hits it twice over: ESPN's player-level ``proTeamId`` and Sleeper's
   EMBEDDED ``row["player"]["team"]`` are both current-roster fields. Sleeper's row-level
   ``team`` is the 2025 one. Picking the wrong field moves whole receiving corps between
   offenses and every ratio in the report stays perfectly plausible while being wrong, so the
   choice is pinned by a test and gated at runtime.
2. **Aggregating actuals on a season-level team.** A mid-season trade must split across two
   offenses, which is only possible from the per-WEEK ``proTeamId``.
3. **A two-sided "renormalization".** Scaling a team's receivers UP because the source
   published few of them is not the proposal and is not defensible; the one-sided variant has
   to actually be one-sided.
4. **A remedy that is really just a haircut.** The level-matched null must remove exactly the
   same league-wide total from exactly the same players, or the decisive comparison is rigged.
5. **Mutating a source's statline in place.** The board must be able to score raw and corrected
   projections in the same process; a remedy that mutates its input silently corrupts the
   baseline it is being compared against.

Nothing here touches the network. The end-to-end test reads the ``data/backtest/`` cache and
skips when it is absent.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

# tools/ is a plain directory of scripts, not an installed package, so load by path.
_spec = importlib.util.spec_from_file_location(
    "check_renormalization", REPO_ROOT / "tools" / "check_renormalization.py"
)
assert _spec and _spec.loader
cr = importlib.util.module_from_spec(_spec)
sys.modules["check_renormalization"] = cr
_spec.loader.exec_module(cr)


# ============================================================================ team plumbing


def test_normalize_team_folds_the_abbreviations_that_actually_differ():
    # Sleeper writes WAS, ESPN writes WSH. Left unfolded, Washington's projections and
    # Washington's actuals land in two different buckets and both look half-sized.
    assert cr.normalize_team("WAS") == "WSH"
    assert cr.normalize_team("was") == "WSH"
    assert cr.normalize_team("JAC") == "JAX"
    assert cr.normalize_team("KC") == "KC"
    assert cr.normalize_team(None) == ""


def test_sleeper_team_comes_from_the_row_not_the_embedded_player():
    """The regression guard for the central trap.

    A player who was on PHI in 2025 and is on NE in 2026 arrives with the 2025 team on the
    projection ROW and the 2026 team on the embedded player object. Reading the embedded one is
    the documented offseason-mover error wearing a different key name.
    """
    rows = [
        {
            "player_id": "1",
            "team": "PHI",
            "player": {"first_name": "A.J.", "last_name": "Brown", "team": "NE"},
        }
    ]

    assert cr.sleeper_projection_teams(rows) == {"1": "PHI"}
    assert cr.sleeper_embedded_teams(rows) == {"1": "NE"}


def test_actual_totals_split_a_midseason_trade_across_both_offenses():
    payload = [
        {
            "player": {
                "id": 7,
                "fullName": "Traded Receiver",
                "defaultPositionId": 3,
                "proTeamId": 1,  # his CURRENT team; must not be used for aggregation
                "stats": [
                    # week on ATL (proTeamId 1): 100 receiving yards
                    {
                        "seasonId": cr.SEASON,
                        "statSourceId": 0,
                        "statSplitTypeId": 1,
                        "proTeamId": 1,
                        "stats": {"42": 100.0, "53": 6.0},
                    },
                    # week on BUF (proTeamId 2): 40 receiving yards
                    {
                        "seasonId": cr.SEASON,
                        "statSourceId": 0,
                        "statSplitTypeId": 1,
                        "proTeamId": 2,
                        "stats": {"42": 40.0, "53": 3.0},
                    },
                    # a PROJECTION block, which must never enter the actuals
                    {
                        "seasonId": cr.SEASON,
                        "statSourceId": 1,
                        "statSplitTypeId": 0,
                        "proTeamId": 1,
                        "stats": {"42": 9_999.0},
                    },
                ],
            }
        }
    ]

    totals = cr.actual_side(payload).team_totals()

    assert totals["ATL"]["rec_yd"] == pytest.approx(100.0)
    assert totals["BUF"]["rec_yd"] == pytest.approx(40.0)
    assert 9_999.0 not in totals["ATL"].values()


def test_top_k_total_is_the_depth_matched_comparison():
    side = cr.ActualSide(
        player_team={
            ("1", "KC"): {"rec_yd": 1000.0},
            ("2", "KC"): {"rec_yd": 600.0},
            ("3", "KC"): {"rec_yd": 200.0},
            ("4", "KC"): {"rec_yd": 0.0},
        }
    )

    # A source that published two receivers for KC is compared against KC's top two, not
    # against the whole offense -- otherwise its roster depth reads as pessimism.
    assert side.top_k_total("KC", "rec_yd", 2) == pytest.approx(1600.0)
    assert side.top_k_total("KC", "rec_yd", 99) == pytest.approx(1800.0)
    assert side.top_k_total("KC", "rec_yd", 0) == pytest.approx(0.0)
    assert side.player_count("KC", "rec_yd") == 3  # the 0.0 row is not a receiver


# ================================================================================ remedies


def _one_team_totals(pass_yd: float, rec_yd: float) -> dict[str, dict[str, float]]:
    return {
        "KC": {
            "pass_cmp": 400.0,
            "pass_yd": pass_yd,
            "pass_td": 30.0,
            "rec": 440.0,
            "rec_yd": rec_yd,
            "rec_td": 33.0,
        }
    }


def test_rec_down_closes_the_identity_exactly():
    totals = _one_team_totals(pass_yd=4000.0, rec_yd=4400.0)

    factors = cr.team_factors(totals, "rec_down")

    assert factors["KC"]["rec_yd"] == pytest.approx(4000.0 / 4400.0)
    assert factors["KC"]["pass_yd"] == pytest.approx(1.0)
    # Applied, the team's receiving total equals its passing total. That is the whole claim.
    assert 4400.0 * factors["KC"]["rec_yd"] == pytest.approx(4000.0)


def test_pass_up_and_split_target_the_other_two_reconciliations():
    totals = _one_team_totals(pass_yd=4000.0, rec_yd=4400.0)

    up = cr.team_factors(totals, "pass_up")["KC"]
    assert up["pass_yd"] == pytest.approx(4400.0 / 4000.0)
    assert up["rec_yd"] == pytest.approx(1.0)
    assert 4000.0 * up["pass_yd"] == pytest.approx(4400.0)

    split = cr.team_factors(totals, "split")["KC"]
    midpoint = 0.5 * (4000.0 + 4400.0)
    assert 4000.0 * split["pass_yd"] == pytest.approx(midpoint)
    assert 4400.0 * split["rec_yd"] == pytest.approx(midpoint)


def test_the_over_variants_are_actually_one_sided():
    """A team whose receiving side is BELOW its own passing side must be left alone.

    That case is usually receiver-depth truncation, not a projection error, and the two-sided
    rule scales those receivers UP by as much as 58% on the real feeds -- which the proposal
    never asked for and ``valuation/envelope.py`` explicitly declines to treat as a violation.
    """
    under = _one_team_totals(pass_yd=4000.0, rec_yd=3600.0)

    two_sided = cr.team_factors(under, "rec_down")["KC"]
    one_sided = cr.team_factors(under, "rec_down_over")["KC"]

    assert two_sided["rec_yd"] > 1.0            # scales the receivers UP
    assert one_sided["rec_yd"] == pytest.approx(1.0)   # leaves them alone

    over = _one_team_totals(pass_yd=4000.0, rec_yd=4400.0)
    assert cr.team_factors(over, "rec_down_over")["KC"]["rec_yd"] < 1.0
    assert cr.team_factors(over, "pass_up_over")["KC"]["pass_yd"] > 1.0
    assert cr.team_factors(under, "pass_up_over")["KC"]["pass_yd"] == pytest.approx(1.0)


def test_a_zero_side_gets_no_factor_rather_than_an_invented_one():
    totals = {"KC": {"pass_yd": 0.0, "rec_yd": 4000.0, "pass_cmp": 0.0, "rec": 400.0}}

    for remedy in cr.REMEDY_SPECS:
        factors = cr.team_factors(totals, remedy)["KC"]
        assert factors["rec_yd"] == pytest.approx(1.0)
        assert factors["pass_yd"] == pytest.approx(1.0)


def test_unknown_remedy_raises_rather_than_silently_doing_nothing():
    with pytest.raises(KeyError):
        cr.team_factors(_one_team_totals(4000.0, 4400.0), "scale_it_a_bit")


def test_apply_factors_preserves_shares_touches_only_the_identity_and_never_mutates():
    line = {
        "rec": 100.0,
        "rec_yd": 1200.0,
        "rec_td": 8.0,
        "rec_tgt": 150.0,     # outside the identity
        "rush_yd": 60.0,      # outside the identity
        "games": 17.0,        # outside the identity
    }
    original = dict(line)
    factors = {"rec": 0.5, "rec_yd": 0.5, "rec_td": 0.5}

    out = cr.apply_factors(line, factors)

    assert out["rec_yd"] == pytest.approx(600.0)
    assert out["rec"] == pytest.approx(50.0)
    assert out["rec_td"] == pytest.approx(4.0)
    # Everything the remedy has no business touching is byte-identical.
    assert out["rec_tgt"] == pytest.approx(150.0)
    assert out["rush_yd"] == pytest.approx(60.0)
    assert out["games"] == pytest.approx(17.0)
    # And the input is untouched, so a raw-vs-corrected comparison stays honest.
    assert line == original


def test_apply_factors_with_no_factors_is_a_copy_not_the_same_object():
    line = {"rec_yd": 1000.0}

    out = cr.apply_factors(line, None)

    assert out == line
    assert out is not line


def test_shares_within_a_team_are_preserved_by_construction():
    pset = cr.ProjectionSet(
        name="t",
        lines={
            "a": {"rec_yd": 1200.0, "rec": 90.0, "rec_td": 8.0},
            "b": {"rec_yd": 800.0, "rec": 70.0, "rec_td": 5.0},
            "qb": {"pass_yd": 1800.0, "pass_cmp": 150.0, "pass_td": 12.0},
        },
        teams={"a": "KC", "b": "KC", "qb": "KC"},
    )

    corrected, _factors = cr.corrected_lines(pset, "rec_down")

    before = 1200.0 / 800.0
    after = corrected["a"]["rec_yd"] / corrected["b"]["rec_yd"]
    assert after == pytest.approx(before)
    assert corrected["a"]["rec_yd"] + corrected["b"]["rec_yd"] == pytest.approx(1800.0)


# ============================================================================ the null test


def test_level_matched_null_removes_the_same_total_but_flat():
    """The control the verdict turns on.

    Same league-wide reduction, same players touched, distributed as one uniform multiplier
    instead of per team. If this were not exactly level-matched, an identity remedy could beat
    it (or lose to it) purely on how much it cut.
    """
    pset = cr.ProjectionSet(
        name="t",
        lines={
            "a": {"rec_yd": 4400.0},
            "b": {"rec_yd": 3000.0},
            "qa": {"pass_yd": 4000.0},
            "qb": {"pass_yd": 4000.0},
        },
        teams={"a": "KC", "qa": "KC", "b": "BUF", "qb": "BUF"},
    )

    identity, _ = cr.corrected_lines(pset, "rec_down_over")
    null, null_factors = cr.corrected_lines(pset, "rec_flat")

    total_identity = sum(line.get("rec_yd", 0.0) for line in identity.values())
    total_null = sum(line.get("rec_yd", 0.0) for line in null.values())
    assert total_null == pytest.approx(total_identity)

    # Uniform: every team gets the same multiplier, which is the point of the null.
    multipliers = {round(f["rec_yd"], 10) for f in null_factors.values()}
    assert len(multipliers) == 1
    # ...and it differs from the per-team treatment, or the comparison would be vacuous.
    assert identity["a"]["rec_yd"] != pytest.approx(null["a"]["rec_yd"])


def test_every_remedy_including_the_nulls_is_reachable():
    pset = cr.ProjectionSet(
        name="t",
        lines={"a": {"rec_yd": 4400.0}, "q": {"pass_yd": 4000.0}},
        teams={"a": "KC", "q": "KC"},
    )

    for remedy in cr.REMEDIES:
        corrected, factors = cr.corrected_lines(pset, remedy)
        assert set(corrected) == {"a", "q"}
        assert factors  # a remedy that produced no factors would silently be a no-op


# ============================================================================== diagnostics


def test_identity_gaps_sign_convention_is_receiving_minus_passing():
    pset = cr.ProjectionSet(
        name="t",
        lines={"a": {"rec_yd": 4400.0, "rec": 440.0}, "q": {"pass_yd": 4000.0, "pass_cmp": 400.0}},
        teams={"a": "KC", "q": "KC"},
    )

    gaps = cr.identity_gaps(pset)

    assert gaps["KC"]["rec_yd"] == pytest.approx(0.10)   # receiving 10% ABOVE passing
    assert gaps["KC"]["rec"] == pytest.approx(0.10)


def test_espn_projection_set_reads_only_the_season_total_projection_block():
    payload = [
        {
            "player": {
                "id": 5,
                "fullName": "Somebody",
                "defaultPositionId": 3,
                "proTeamId": 1,
                "stats": [
                    {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 0,
                     "stats": {"42": 1000.0}},
                    {"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0,
                     "stats": {"42": 1100.0}},
                    {"seasonId": 2025, "statSourceId": 0, "statSplitTypeId": 0,
                     "stats": {"42": 900.0}},     # actual, not a projection
                    {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 1,
                     "stats": {"42": 60.0}},      # weekly, not a season total
                ],
            }
        }
    ]

    assert cr.espn_projection_set_for_season(payload, 2025).lines["5"]["rec_yd"] == 1000.0
    assert cr.espn_projection_set_for_season(payload, 2026).lines["5"]["rec_yd"] == 1100.0


def test_espn_projection_set_skips_kickers_and_defenses():
    payload = [
        {"player": {"id": 1, "defaultPositionId": 5, "proTeamId": 1, "stats": [
            {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 0, "stats": {"42": 5.0}}]}},
        {"player": {"id": 2, "defaultPositionId": 16, "proTeamId": 1, "stats": [
            {"seasonId": 2025, "statSourceId": 1, "statSplitTypeId": 0, "stats": {"42": 5.0}}]}},
    ]

    assert cr.espn_projection_set_for_season(payload, 2025).lines == {}


# =================================================================================== gates


def test_identity_closure_gate_rejects_a_spine_that_cannot_close_the_identity():
    """A spine that violates the identity cannot be used to decide who violates it.

    This is exactly why the report uses the 1000-player payload: the 700-player one puts NYJ's
    real receiving 23% above its own real completions, which is arithmetically impossible and
    is purely a missing quarterback.
    """
    broken = cr.ActualSide(
        player_team={
            ("qb", "NYJ"): {"pass_cmp": 100.0, "pass_yd": 1000.0, "pass_td": 5.0},
            ("wr", "NYJ"): {"rec": 123.0, "rec_yd": 1230.0, "rec_td": 6.0},
        }
    )

    # Reported without gating...
    lines = cr.identity_closure_lines(broken, gate=False)
    assert any("+23.00%" in line for line in lines)

    # ...and refused when it is the spine actually being used.
    with pytest.raises(AssertionError, match="does not close"):
        cr.identity_closure_lines(broken, gate=True)


def test_identity_closure_gate_accepts_a_spine_that_closes():
    good = cr.ActualSide(
        player_team={
            ("qb", "KC"): {"pass_cmp": 400.0, "pass_yd": 4000.0, "pass_td": 30.0},
            ("wr", "KC"): {"rec": 401.0, "rec_yd": 4010.0, "rec_td": 32.0},
        }
    )

    lines = cr.identity_closure_lines(good, gate=True)

    # The touchdown row is reported and never gated: one TD on a 30-TD team is 3%, and
    # valuation/envelope.py fits its own TD tolerance at 6.67% for the same reason.
    assert len(lines) == len(cr.IDENTITY_PAIRS)


def test_team_vintage_gate_fails_when_the_projection_payload_serves_current_rosters():
    """If the 2025 payload's teams matched the 2026 payload's, it would be serving 2026
    rosters and there would be no 2025 attribution left to use. The gate must say so rather
    than quietly producing plausible garbage."""
    same_team_everywhere = [
        {
            "player": {
                "id": 1,
                "fullName": "Anyone",
                "defaultPositionId": 3,
                "proTeamId": 1,
                "stats": [
                    {"seasonId": cr.SEASON, "statSourceId": 0, "statSplitTypeId": 1,
                     "proTeamId": 1, "stats": {"42": 50.0}},
                ],
            }
        }
    ]
    original = cr.espn_current_teams_2026
    cr.espn_current_teams_2026 = lambda: {"1": "ATL"}  # identical to the 2025 payload
    try:
        with pytest.raises(AssertionError, match="serving current rosters"):
            cr.team_vintage_lines(same_team_everywhere, [])
    finally:
        cr.espn_current_teams_2026 = original


# ============================================================================== end to end


@pytest.mark.skipif(
    not (cr.bt.ESPN_CACHE.exists() and cr.bt.SLEEPER_CACHE.exists() and cr.bt.FFC_CACHE.exists()),
    reason="data/backtest/ cache absent; run tools/backtest_sources.py --refresh once",
)
def test_report_runs_end_to_end_against_the_real_cache():
    report = cr.build_report()

    # Every gate actually ran, and the report says which spine it used.
    assert "GATE 1: ESPN stat-id identities" in report
    assert "GATE 2: TEAM ATTRIBUTION IS 2025" in report
    assert "1000-player window)  <- USED" in report
    # Both questions answered, and the decisive control is present.
    assert "Q1. WHICH SIDE IS ACTUALLY WRONG?" in report
    assert "Q2. WOULD IT HAVE IMPROVED" in report
    assert "THE NULL TEST" in report
    assert "VERDICT, computed from the run above" in report
    # Every source x remedy cell exists, so no column is quietly missing.
    for source in cr.SOURCES:
        for remedy in cr.REMEDIES:
            assert remedy in report
        assert source in report


@pytest.mark.skipif(
    not cr.bt.ESPN_CACHE.exists(), reason="data/backtest/ cache absent"
)
def test_the_2025_spine_actually_closes_the_identity_on_real_data():
    """The one real-data invariant this whole verdict rests on."""
    actuals = cr.actual_side(cr._espn_2026_players())
    team_totals = actuals.team_totals()

    assert len(team_totals) == 32
    for team, totals in team_totals.items():
        assert totals["pass_yd"] > 0
        assert abs(totals["rec_yd"] - totals["pass_yd"]) / totals["pass_yd"] < 0.02, team
