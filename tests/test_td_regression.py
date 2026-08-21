"""Tests for the TD-regression flag (docs/PLAN_2026-08-20.md, B4).

The hand-built cases prove the two things that would make this mechanism dangerous rather than
useless if they broke: a projection with obviously too many touchdowns for its yardage must be
flagged, and an ordinary one must not. The estimator test pins the choice of the pooled
(Poisson-MLE) slope over OLS, which is not cosmetic -- OLS ran hot enough on the real 2025 data
to invent a 20-30% "under-projection" of quarterback rushing touchdowns that did not exist.

Real-cached-data tests at the bottom pin the fitted numbers and the predictor choice. They skip
rather than fail when the cache is absent; no test here touches the network.
"""

from __future__ import annotations

import pytest

from draftroom.prep.schema import StatLine
from draftroom.valuation import td_regression as tdr

# --------------------------------------------------------------------------- fixtures

WR_RECTD = {("WR", "rec_td"): ("rec_yd",)}
TRUE_RATE = 0.006  # 0.6 receiving TDs per 100 yards


def actual(pid: str, pos: str, **stats: float) -> tdr.PlayerActual:
    return tdr.PlayerActual(player_id=pid, name=f"P{pid}", pos=pos, games=17, stats=stats)


def noisy_wr_sample(n: int = 80) -> list[tdr.PlayerActual]:
    """``n`` receivers around ``TRUE_RATE`` with alternating +-1 TD of real, integer noise."""
    out = []
    for i in range(n):
        yards = 300.0 + 10.0 * i
        noise = 1.0 if i % 2 else -1.0
        out.append(actual(str(i), "WR", rec_yd=yards, rec_td=max(TRUE_RATE * yards + noise, 0.0)))
    return out


# --------------------------------------------------------------------------- the estimator


def test_pooled_slope_is_not_ols_and_both_are_reported():
    """``pooled = sum(y)/sum(x)``, ``ols = sum(xy)/sum(x^2)``. Hand-checkable on two points."""
    pooled, ols, r2 = tdr._fit_through_origin([1.0, 2.0], [1.0, 4.0])
    assert pooled == pytest.approx(5.0 / 3.0)
    assert ols == pytest.approx(9.0 / 5.0)
    assert r2 == pytest.approx(tdr._r2_through_origin([1.0, 2.0], [1.0, 4.0], pooled))


def test_ols_runs_hot_when_the_high_volume_players_score_faster():
    """The exact bias that made the aggregate check lie on the real data: give the biggest
    player a higher rate and OLS chases him while the pooled rate does not."""
    xs = [100.0, 100.0, 100.0, 1000.0]
    ys = [0.5, 0.5, 0.5, 10.0]  # small players at 0.005/yd, the big one at 0.010/yd
    pooled, ols, _ = tdr._fit_through_origin(xs, ys)
    assert pooled == pytest.approx(11.5 / 1300.0)
    assert ols > pooled
    assert ols / pooled > 1.05


def test_a_perfect_rate_sample_recovers_the_rate_exactly():
    sample = noisy_wr_sample()
    # replace the noise with none: the pooled rate must come back as TRUE_RATE
    exact = [
        actual(a.player_id, "WR", rec_yd=a.get("rec_yd"), rec_td=TRUE_RATE * a.get("rec_yd"))
        for a in sample
    ]
    models = tdr.fit_td_models(exact, seasons=(2025,), candidates=WR_RECTD)
    model = models.models[("WR", "rec_td")]
    assert model.slope == pytest.approx(TRUE_RATE)
    assert model.r2 == pytest.approx(1.0)
    assert model.dispersion == pytest.approx(0.0, abs=1e-12)


def test_a_zero_dispersion_fit_flags_nothing_rather_than_everything():
    """A degenerate fit has no measurable spread, so every |z| threshold is undefined. The
    honest behaviour is to flag nobody -- dividing by a zero SD would flag the entire board."""
    exact = [
        actual(str(i), "WR", rec_yd=300.0 + 10.0 * i, rec_td=TRUE_RATE * (300.0 + 10.0 * i))
        for i in range(80)
    ]
    models = tdr.fit_td_models(exact, seasons=(2025,), candidates=WR_RECTD)
    statlines = {"x": StatLine(rec_yd=1000.0, rec_td=25.0)}
    assert tdr.flag_statlines(statlines, {"x": "WR"}, models) == ()


# --------------------------------------------------------------------------- the fit itself


def test_predictor_is_chosen_by_r2_and_every_candidate_is_recorded():
    """rec_yd is built to carry the signal; rec is built to be pure noise. The fit must pick
    rec_yd, and must still report what rec scored so the choice is auditable."""
    rows = []
    for i in range(80):
        yards = 300.0 + 10.0 * i
        rows.append(
            actual(str(i), "WR", rec_yd=yards, rec=50.0, rec_td=TRUE_RATE * yards)
        )
    models = tdr.fit_td_models(
        rows, seasons=(2025,), candidates={("WR", "rec_td"): ("rec_yd", "rec")}
    )
    model = models.models[("WR", "rec_td")]
    assert model.predictor == "rec_yd"
    assert set(model.candidate_r2) == {"rec_yd", "rec"}
    assert model.candidate_r2["rec_yd"] > model.candidate_r2["rec"]


def test_usage_floor_is_the_median_of_nonzero_values():
    rows = noisy_wr_sample(n=80)
    models = tdr.fit_td_models(rows, seasons=(2025,), candidates=WR_RECTD)
    model = models.models[("WR", "rec_td")]
    yards = sorted(a.get("rec_yd") for a in rows)
    expected_floor = (yards[39] + yards[40]) / 2.0
    assert model.usage_floor == pytest.approx(expected_floor)
    assert model.n == sum(1 for y in yards if y >= expected_floor)
    assert "median of non-zero rec_yd" in model.usage_floor_rule


def test_a_group_with_too_few_rows_gets_no_model_and_says_so():
    rows = [actual(str(i), "WR", rec_yd=500.0, rec_td=3.0) for i in range(10)]
    models = tdr.fit_td_models(rows, seasons=(2025,), candidates=WR_RECTD)
    assert ("WR", "rec_td") not in models.models
    assert "WR/rec_td" in models.provenance["skipped"]


def test_seasons_and_caveat_travel_with_the_models():
    models = tdr.fit_td_models(noisy_wr_sample(), seasons=(2025,), candidates=WR_RECTD)
    assert models.models[("WR", "rec_td")].seasons == (2025,)
    assert "ONE season" in models.caveat
    assert models.provenance["seasons"] == (2025,)


# --------------------------------------------------------------------------- flagging


@pytest.fixture
def noisy_models() -> tdr.TdModelSet:
    return tdr.fit_td_models(noisy_wr_sample(), seasons=(2025,), candidates=WR_RECTD)


def test_an_absurd_touchdown_projection_is_flagged(noisy_models):
    """1,000 receiving yards and 20 touchdowns. Nothing in the fitted sample is remotely that
    far from its own yardage, so this must trip the threshold."""
    flags = tdr.flag_statlines(
        {"boom": StatLine(rec_yd=1000.0, rec_td=20.0)}, {"boom": "WR"}, noisy_models
    )
    assert len(flags) == 1
    flag = flags[0]
    assert flag.td_stat == "rec_td"
    assert flag.direction == "high"
    assert flag.expected_td == pytest.approx(noisy_models.models[("WR", "rec_td")].slope * 1000.0)
    assert flag.z > flag.threshold
    assert flag.delta > 0


def test_an_ordinary_touchdown_projection_is_not_flagged(noisy_models):
    """Same yardage, touchdowns right on the fitted rate. Must be silent."""
    model = noisy_models.models[("WR", "rec_td")]
    ordinary = StatLine(rec_yd=1000.0, rec_td=model.slope * 1000.0)
    assert tdr.flag_statlines({"ok": ordinary}, {"ok": "WR"}, noisy_models) == ()


def test_a_low_touchdown_projection_is_flagged_in_the_other_direction(noisy_models):
    flags = tdr.flag_statlines(
        {"cold": StatLine(rec_yd=1400.0, rec_td=0.0)}, {"cold": "WR"}, noisy_models
    )
    assert len(flags) == 1
    assert flags[0].direction == "low"
    assert flags[0].z < 0


def test_a_player_below_the_usage_floor_is_skipped_not_flagged(noisy_models):
    """The model was never fitted on players that small, so it has nothing to say about them.
    Silence is the correct output, not a flag and not a fabricated expectation."""
    model = noisy_models.models[("WR", "rec_td")]
    tiny = StatLine(rec_yd=model.usage_floor - 1.0, rec_td=9.0)
    assert tdr.flag_statlines({"tiny": tiny}, {"tiny": "WR"}, noisy_models) == ()


def test_a_looser_quantile_flags_more(noisy_models):
    statlines = {
        str(i): StatLine(rec_yd=1000.0, rec_td=noisy_models.models[("WR", "rec_td")].slope * 1000.0 + d)
        for i, d in enumerate((2.0, 3.0, 4.0, 5.0))
    }
    pos = {k: "WR" for k in statlines}
    at_95 = tdr.flag_statlines(statlines, pos, noisy_models, quantile=0.95)
    at_50 = tdr.flag_statlines(statlines, pos, noisy_models, quantile=0.50)
    assert len(at_50) >= len(at_95)


def test_flags_are_sorted_by_absolute_z(noisy_models):
    statlines = {
        "a": StatLine(rec_yd=1000.0, rec_td=14.0),
        "b": StatLine(rec_yd=1000.0, rec_td=25.0),
    }
    flags = tdr.flag_statlines(statlines, {"a": "WR", "b": "WR"}, noisy_models)
    assert [f.player_id for f in flags] == ["b", "a"]


# --------------------------------------------------------------------------- aggregate bias


def test_source_bias_is_exactly_one_when_the_source_matches_the_fitted_rate(noisy_models):
    model = noisy_models.models[("WR", "rec_td")]
    statlines = {
        str(i): StatLine(rec_yd=y, rec_td=model.slope * y)
        for i, y in enumerate((900.0, 1100.0, 1300.0))
    }
    bias = tdr.source_bias("fake", statlines, {k: "WR" for k in statlines}, noisy_models)
    assert len(bias) == 1
    assert bias[0].ratio == pytest.approx(1.0)
    assert bias[0].z == pytest.approx(0.0)
    assert bias[0].n_players == 3


def test_source_bias_catches_a_source_wide_touchdown_inflation(noisy_models):
    model = noisy_models.models[("WR", "rec_td")]
    statlines = {
        str(i): StatLine(rec_yd=y, rec_td=model.slope * y * 1.5)
        for i, y in enumerate(900.0 + 20.0 * i for i in range(60))
    }
    bias = tdr.source_bias("hot", statlines, {k: "WR" for k in statlines}, noisy_models)[0]
    assert bias.ratio == pytest.approx(1.5)
    assert bias.z > 3.0


def test_source_bias_ignores_players_below_the_usage_floor(noisy_models):
    model = noisy_models.models[("WR", "rec_td")]
    statlines = {"tiny": StatLine(rec_yd=model.usage_floor - 1.0, rec_td=99.0)}
    assert tdr.source_bias("x", statlines, {"tiny": "WR"}, noisy_models) == ()


# --------------------------------------------------------------------------- calibration backtest


def test_calibration_compares_rates_not_totals(noisy_models):
    """A projection that nails the RATE but overshoots the yardage (nobody knows who gets hurt)
    must come back calibrated. Comparing totals instead would call it 2x too aggressive."""
    model = noisy_models.models[("WR", "rec_td")]
    rate = model.slope
    projections = {"p": StatLine(rec_yd=1400.0, rec_td=rate * 1400.0)}
    actuals = {"p": actual("p", "WR", rec_yd=700.0, rec_td=rate * 700.0)}
    cal = tdr.backtest_rate_calibration("s", projections, actuals, noisy_models, season=2025)[0]
    assert cal.rate_ratio == pytest.approx(1.0)
    assert cal.projected_td / cal.actual_td == pytest.approx(2.0)  # totals would have lied


def test_calibration_detects_an_under_projected_rate(noisy_models):
    model = noisy_models.models[("WR", "rec_td")]
    projections = {"p": StatLine(rec_yd=1000.0, rec_td=model.slope * 1000.0 * 0.8)}
    actuals = {"p": actual("p", "WR", rec_yd=1000.0, rec_td=model.slope * 1000.0)}
    cal = tdr.backtest_rate_calibration("s", projections, actuals, noisy_models, season=2025)[0]
    assert cal.rate_ratio == pytest.approx(0.8)


def test_calibration_eligibility_uses_the_projected_predictor(noisy_models):
    """Selecting on the ACTUAL predictor would condition on the outcome and flatter the
    projection. A player projected below the floor must be excluded even if he blew up."""
    model = noisy_models.models[("WR", "rec_td")]
    projections = {"p": StatLine(rec_yd=model.usage_floor - 1.0, rec_td=0.5)}
    actuals = {"p": actual("p", "WR", rec_yd=1500.0, rec_td=12.0)}
    assert tdr.backtest_rate_calibration("s", projections, actuals, noisy_models, season=2025) == ()


# --------------------------------------------------------------------------- real cached data


@pytest.fixture(scope="module")
def real_models():
    from draftroom.prep.http import load_latest_raw

    try:
        raw = load_latest_raw("espn")
    except FileNotFoundError:
        pytest.skip("no cached ESPN payload under data/raw/espn/")
    actuals = tdr.player_season_actuals(raw, 2025)
    return actuals, tdr.fit_td_models(actuals, seasons=(2025,))


def test_real_fit_produces_a_model_for_every_group(real_models):
    _, models = real_models
    assert set(models.models) == set(tdr.CANDIDATE_PREDICTORS)
    assert models.provenance["skipped"] == ()


def test_real_fit_rates_are_football_shaped(real_models):
    """Sanity, not precision: passing TDs land near 0.7 per 100 yards, receiving near 0.6, and
    every group's dispersion is in Poisson territory rather than orders of magnitude off."""
    _, models = real_models
    qb = models.models[("QB", "pass_td")]
    assert 0.4 < qb.slope * 100 < 1.0
    assert qb.r2 > 0.7
    for model in models.models.values():
        assert 0.1 < model.dispersion < 5.0, model
        assert model.n >= tdr.MIN_FIT_ROWS


def test_receiving_models_never_predict_off_targets(real_models):
    """The coverage decision, pinned. Targets exist in only one of the three source families,
    so a targets-based predictor makes the flag blind to two thirds of the board."""
    _, models = real_models
    for (pos, td_stat), model in models.models.items():
        assert model.predictor != "rec_tgt", (pos, td_stat)


def test_real_receiving_fits_are_weak_enough_to_stay_a_flag(real_models):
    """The number that decides whether this mechanism earns automatic rejection. Yardage
    explains roughly half of who scores; if that ever changes, revisit the decision."""
    _, models = real_models
    for pos in ("WR", "TE", "RB"):
        model = models.models[(pos, "rec_td")]
        assert model.r2 < 0.7, f"{pos} rec_td R2 rose to {model.r2:.3f} -- re-read the caveat"


def test_ols_would_have_run_hot_on_the_real_data(real_models):
    """Documents why the pooled slope is used. On QB rushing TDs OLS is ~15% above pooled,
    which was enough to fabricate a source-wide 'under-projection' that did not exist."""
    _, models = real_models
    qb_rush = models.models[("QB", "rush_td")]
    assert qb_rush.ols_slope / qb_rush.slope > 1.05
