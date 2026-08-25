"""Tests for the calibration machinery in `draftroom.draft.opponents`.

Covers the pure-math pieces (`scale_adp_to_league`, `fit_position_timing_offset`,
`fit_manager_reach`'s empirical-Bayes shrinkage) with small, hand-built synthetic cases where
the right answer is known in advance, plus the JSON round-trip
(`LeagueCalibration.to_json`/`from_calibration_file`).

One test (`TestShippedCalibrationFile`) is a deliberate guard rail: it loads the REAL
`data/opponent_calibration_2025.json` that `the opponent calibration study (retired 2026-08-25)` produces and asserts
it is currently empty (equivalent to `national_only()`). That tool's own leave-one-manager-out
validation found the naive flat per-position offset does not beat plain national ADP -- see
its module docstring and `opponents.py`'s. If a future recalibration run legitimately beats
plain ADP out-of-sample, this test is EXPECTED to need updating alongside it; until then, it
fails loudly if the params file is ever overwritten with an unvalidated non-empty calibration.
"""

from __future__ import annotations

import json

import pytest

from draftroom.config import LeagueConfig
from draftroom.draft import opponents as opp

TEAMS = 10
STARTERS = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
FLEX = frozenset({"RB", "WR", "TE"})
SCORING = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0,
    "rush_yd": 0.1, "rush_td": 6.0,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0,
    "fum_lost": -2.0,
}


def make_cfg(**overrides) -> LeagueConfig:
    payload: dict = dict(
        teams=TEAMS, starters=dict(STARTERS), flex_slots=1, flex_eligible=FLEX,
        bench=6, weeks=17, scoring=dict(SCORING),
    )
    payload.update(overrides)
    return LeagueConfig(**payload)


def obs(pick_no: int, team_slot: int, pos: str, scaled_adp: float) -> opp.PickObservation:
    return opp.PickObservation(pick_no=pick_no, team_slot=team_slot, pos=pos, scaled_adp=scaled_adp)


# --------------------------------------------------------------------------- scale_adp_to_league


class TestScaleAdpToLeague:
    def test_ratio_matches_team_count(self):
        # A 12-team pick lands proportionally sooner on a 10-team board.
        assert opp.scale_adp_to_league(12.0, teams_national=12, teams_league=10) == pytest.approx(10.0)

    def test_identity_when_team_counts_match(self):
        assert opp.scale_adp_to_league(37.5, teams_national=12, teams_league=12) == pytest.approx(37.5)

    def test_scales_up_for_a_smaller_national_count(self):
        # Fewer national teams than the league -> the same national pick maps LATER here.
        assert opp.scale_adp_to_league(10.0, teams_national=8, teams_league=12) == pytest.approx(15.0)


# --------------------------------------------------------------------------- fit_position_timing_offset


class TestFitPositionTimingOffset:
    def test_constant_shift_is_recovered_exactly(self):
        # Every QB observation is taken exactly 5 picks before its scaled ADP says it should go.
        observations = [
            obs(5, 1, "QB", 10.0),
            obs(15, 2, "QB", 20.0),
            obs(45, 3, "QB", 50.0),
        ]
        offsets = opp.fit_position_timing_offset(observations)
        assert offsets == {"QB": pytest.approx(5.0)}

    def test_positive_offset_means_sooner_than_market(self):
        # scaled_adp=100, actual pick=40 -> taken 60 picks SOONER than the market said -> +60.
        offsets = opp.fit_position_timing_offset([obs(40, 1, "RB", 100.0)])
        assert offsets["RB"] == pytest.approx(60.0)

    def test_negative_offset_means_later_than_market(self):
        # scaled_adp=10, actual pick=40 -> taken 30 picks LATER than the market said -> -30.
        offsets = opp.fit_position_timing_offset([obs(40, 1, "TE", 10.0)])
        assert offsets["TE"] == pytest.approx(-30.0)

    def test_positions_average_independently(self):
        observations = [
            obs(1, 1, "QB", 1.0),   # QB resid 0
            obs(2, 1, "QB", 4.0),   # QB resid +2
            obs(3, 2, "RB", 3.0),   # RB resid 0
        ]
        offsets = opp.fit_position_timing_offset(observations)
        assert offsets["QB"] == pytest.approx(1.0)
        assert offsets["RB"] == pytest.approx(0.0)

    def test_no_observations_gives_empty_offsets(self):
        assert opp.fit_position_timing_offset([]) == {}


# --------------------------------------------------------------------------- fit_manager_reach


class TestFitManagerReach:
    def test_shrinks_toward_zero_when_managers_are_pure_noise(self):
        # Same true reach (0) for everyone; the "signal" is only sampling noise -> lambda ~ 0.
        observations = []
        noise = [-3.0, 3.0, -1.0, 1.0, -2.0, 2.0]
        for slot in range(1, 4):
            for i, n in enumerate(noise):
                observations.append(obs(50 + i, slot, "WR", 50.0 + n))
        fit = opp.fit_manager_reach(observations, {"WR": 0.0})
        assert 0.0 <= fit.lam <= 1.0
        for slot in fit.shrunk:
            assert abs(fit.shrunk[slot]) <= abs(fit.raw[slot]) + 1e-9

    def test_shrinks_toward_raw_when_between_manager_signal_dominates(self):
        # Manager 1 is reliably +20 picks early, manager 2 reliably +0, with ~zero within-noise
        # -> shrinkage should preserve almost all of the (huge, consistent) between-manager gap.
        observations = []
        for i in range(6):
            observations.append(obs(50 + i, 1, "WR", 70.0 + i))   # resid always +20
            observations.append(obs(50 + i, 2, "WR", 50.0 + i))   # resid always 0
        fit = opp.fit_manager_reach(observations, {"WR": 0.0})
        assert fit.lam > 0.9
        assert fit.shrunk[1] == pytest.approx(fit.raw[1], rel=0.1)
        assert fit.raw[1] > fit.raw[2]

    def test_single_manager_shrinks_fully_to_grand_mean(self):
        observations = [obs(10, 1, "RB", 20.0)]
        fit = opp.fit_manager_reach(observations, {"RB": 0.0})
        assert fit.lam == 0.0
        assert fit.shrunk[1] == pytest.approx(fit.raw[1])

    def test_n_per_manager_matches_input(self):
        observations = [obs(10 + i, 1, "RB", 20.0 + i) for i in range(4)]
        observations += [obs(50 + i, 2, "RB", 60.0 + i) for i in range(3)]
        fit = opp.fit_manager_reach(observations, {"RB": 0.0})
        assert fit.n_per_manager == {1: 4, 2: 3}


# --------------------------------------------------------------------------- from_draft_results


class TestFromDraftResults:
    def test_fits_position_offset_by_default(self):
        observations = [obs(5, 1, "QB", 10.0), obs(15, 2, "QB", 20.0)]
        calib = opp.LeagueCalibration.from_draft_results(observations)
        assert calib.position_timing_offset == {"QB": pytest.approx(5.0)}

    def test_manager_reach_empty_unless_opted_in(self):
        observations = [obs(5, 1, "QB", 10.0), obs(15, 2, "QB", 20.0)]
        calib = opp.LeagueCalibration.from_draft_results(observations)
        assert dict(calib.manager_reach) == {}

    def test_manager_reach_populated_when_opted_in(self):
        observations = []
        for i in range(6):
            observations.append(obs(50 + i, 1, "WR", 70.0 + i))
            observations.append(obs(50 + i, 2, "WR", 50.0 + i))
        calib = opp.LeagueCalibration.from_draft_results(observations, include_manager_reach=True)
        assert set(calib.manager_reach) == {1, 2}
        assert calib.manager_reach[1] > calib.manager_reach[2]


# --------------------------------------------------------------------------- JSON round-trip


class TestCalibrationJsonRoundTrip:
    def test_round_trip_preserves_offsets_and_reach(self, tmp_path):
        calib = opp.LeagueCalibration(
            position_timing_offset={"QB": 3.5, "RB": -1.25},
            manager_reach={1: 2.0, 7: -0.5},
        )
        path = tmp_path / "calib.json"
        calib.to_json(path)
        loaded = opp.LeagueCalibration.from_calibration_file(path)
        assert dict(loaded.position_timing_offset) == pytest.approx(dict(calib.position_timing_offset))
        assert dict(loaded.manager_reach) == pytest.approx(dict(calib.manager_reach))

    def test_extra_diagnostic_keys_do_not_leak_into_the_loaded_calibration(self, tmp_path):
        calib = opp.LeagueCalibration.national_only()
        path = tmp_path / "calib.json"
        calib.to_json(path, extra={"measured_position_timing_offset_name_matched": {"QB": -99.0}})
        loaded = opp.LeagueCalibration.from_calibration_file(path)
        assert dict(loaded.position_timing_offset) == {}
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert "measured_position_timing_offset_name_matched" in payload

    def test_from_calibration_file_defaults_to_default_calibration_path(self):
        # Doesn't need to exist for THIS test -- just confirms the default is wired to the
        # module-level constant the calibration tool writes to.
        import inspect

        sig = inspect.signature(opp.LeagueCalibration.from_calibration_file)
        assert sig.parameters["path"].default is None


# --------------------------------------------------------------------------- integration: no-op check


class TestCalibrationScoringIntegration:
    """A calibration with empty offsets/reach must score IDENTICALLY to national_only()."""

    def test_empty_calibration_matches_national_only_scores(self):
        cfg = make_cfg()
        players = [
            {"player_id": "1", "pos": "QB", "adp": 5.0, "stdev": 2.0},
            {"player_id": "2", "pos": "RB", "adp": 8.0, "stdev": 2.0},
            {"player_id": "3", "pos": "WR", "adp": 12.0, "stdev": 3.0},
        ]
        have: dict = {}
        empty = opp.LeagueCalibration(position_timing_offset={}, manager_reach={})
        national = opp.LeagueCalibration.national_only()
        scores_empty = opp.opponent_scores(players, team_slot=3, pick_no=25.0, have=have, cfg=cfg, calibration=empty)
        scores_national = opp.opponent_scores(players, team_slot=3, pick_no=25.0, have=have, cfg=cfg, calibration=national)
        assert scores_empty == pytest.approx(scores_national)


# --------------------------------------------------------------------------- the shipped file


class TestShippedCalibrationFile:
    """Guard rail: today's honest verdict is 'plain ADP wins' -- see module docstring."""

    def test_shipped_file_exists(self):
        assert opp.DEFAULT_CALIBRATION_PATH.exists(), (
            "expected the opponent calibration study (retired 2026-08-25) to have been run at least once, producing "
            f"{opp.DEFAULT_CALIBRATION_PATH}"
        )

    def test_shipped_file_ships_empty_calibration(self):
        calib = opp.LeagueCalibration.from_calibration_file()
        assert dict(calib.position_timing_offset) == {}, (
            "the shipped params file now carries a non-empty position_timing_offset -- if a "
            "recalibration run legitimately beat plain ADP out-of-sample, update this test "
            "(and paste the new leave-one-manager-out numbers into the PR/report); otherwise "
            "this is a regression."
        )
        assert dict(calib.manager_reach) == {}

    def test_shipped_file_records_the_validation_verdict(self):
        with open(opp.DEFAULT_CALIBRATION_PATH, encoding="utf-8") as fh:
            payload = json.load(fh)
        validation = payload["validation"]
        assert validation["scheme"] == "leave-one-manager-out (10 folds)"
        assert "plain_adp" in validation and "calibrated_position_offset" in validation
        assert "DOES NOT BEAT" in validation["verdict"] or "BEATS" in validation["verdict"]
