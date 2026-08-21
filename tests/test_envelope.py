"""Tests for the team-envelope validator (docs/PLAN_2026-08-20.md, B3).

Two kinds of test below, deliberately separated.

*Hand-built fixtures* where the answer is known by construction: a fake team whose projections
obviously cannot exist must be flagged, and a fake team built to sit inside the band must not
be. Those tests are the ones that would catch the validator silently inverting its own
direction convention -- the failure mode that turns this check into 32 meaningless flags.

*Real cached data* smoke tests, which pin the two findings the checks actually produced on the
2026 board so a future change to the aggregation can't quietly lose them. They skip rather than
fail when the cache is absent, because no test in this repo may hit the network.
"""

from __future__ import annotations

import pytest

from draftroom.prep.schema import StatLine
from draftroom.valuation import envelope as env

# --------------------------------------------------------------------------- fixtures


def line(**kwargs: float) -> StatLine:
    return StatLine(**kwargs)


#: A hand-built "real 2025" of four team-seasons. Small on purpose: every band number below is
#: derivable by hand from these four rows, so a test failure points at the code, not at data.
FAKE_ACTUALS: dict[str, dict[str, float]] = {
    "AAA": {"pass_att": 500, "pass_cmp": 330, "pass_yd": 3600, "pass_td": 24,
            "rush_att": 430, "rush_yd": 1800, "rush_td": 14,
            "rec": 330, "rec_tgt": 480, "rec_yd": 3600, "rec_td": 24},
    "BBB": {"pass_att": 550, "pass_cmp": 360, "pass_yd": 4000, "pass_td": 28,
            "rush_att": 450, "rush_yd": 2000, "rush_td": 16,
            "rec": 360, "rec_tgt": 520, "rec_yd": 4000, "rec_td": 28},
    "CCC": {"pass_att": 600, "pass_cmp": 390, "pass_yd": 4400, "pass_td": 30,
            "rush_att": 470, "rush_yd": 2200, "rush_td": 18,
            "rec": 392, "rec_tgt": 560, "rec_yd": 4420, "rec_td": 30},
    "DDD": {"pass_att": 620, "pass_cmp": 400, "pass_yd": 4600, "pass_td": 32,
            "rush_att": 500, "rush_yd": 2400, "rush_td": 20,
            "rec": 400, "rec_tgt": 580, "rec_yd": 4600, "rec_td": 32},
}


def _with_derived(actuals: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    out = {}
    for team, stats in actuals.items():
        row = dict(stats)
        row["total_td"] = row["pass_td"] + row["rush_td"]
        out[team] = row
    return out


#: League means per season, hand-built so the drift is exactly +10% up / -20% down on pass_yd
#: relative to the fit season, and 0 / 0 on the other two.
FAKE_YARDAGE_MEANS = {
    2024: {"pass_yd": 3200.0, "rush_yd": 2000.0, "rec_yd": 3200.0},
    2025: {"pass_yd": 4000.0, "rush_yd": 2000.0, "rec_yd": 4000.0},
    2026: {"pass_yd": 4400.0, "rush_yd": 2000.0, "rec_yd": 4000.0},
}


@pytest.fixture
def bandset() -> env.BandSet:
    return env.fit_bands(
        team_actuals=_with_derived(FAKE_ACTUALS),
        yardage_means=FAKE_YARDAGE_MEANS,
        fit_season=2025,
    )


# --------------------------------------------------------------------------- sum_by_team


def test_sum_by_team_sums_and_names_contributors():
    statlines = {
        "p1": line(rec=80, rec_yd=1100, rec_td=8),
        "p2": line(rec=50, rec_yd=600, rec_td=3),
        "p3": line(rec=20, rec_yd=200, rec_td=1),
    }
    sums = env.sum_by_team(
        statlines,
        {"p1": "AAA", "p2": "AAA", "p3": "BBB"},
        name_of={"p1": "Big", "p2": "Mid", "p3": "Other"},
    )
    assert sums["AAA"].n_players == 2
    assert sums["AAA"].get("rec_yd") == pytest.approx(1700)
    assert sums["BBB"].get("rec_yd") == pytest.approx(200)
    # contributors sorted by value descending, and carrying the display name
    assert [name for _, name, _ in sums["AAA"].contributors["rec_yd"]] == ["Big", "Mid"]


def test_total_td_does_not_double_count_passing_scores():
    """A passing TD and a receiving TD are the SAME touchdown. Summing all three TD stats
    would report ~1.6x a real team's offensive output and make every band look busted."""
    statlines = {
        "qb": line(pass_td=30, rush_td=3),
        "wr": line(rec_td=12),
        "rb": line(rush_td=10, rec_td=4),
    }
    sums = env.sum_by_team(statlines, {"qb": "AAA", "wr": "AAA", "rb": "AAA"})
    assert sums["AAA"].get("pass_td") == 30
    assert sums["AAA"].get("rec_td") == 16
    assert sums["AAA"].get("total_td") == pytest.approx(43)  # 30 pass + 13 rush, NOT 59


def test_contributor_counts_are_untruncated():
    """``contributors`` is truncated for display; ``contributor_counts`` is not, because the
    identity check's confound test needs to know how many passers a source actually published,
    not just the top five."""
    statlines = {str(i): line(rec=10.0 + i, rec_yd=100.0) for i in range(9)}
    sums = env.sum_by_team(statlines, {str(i): "AAA" for i in range(9)}, top_n=3)
    assert len(sums["AAA"].contributors["rec"]) == 3
    assert sums["AAA"].count("rec") == 9
    assert sums["AAA"].count("pass_cmp") == 0


def test_unattributable_players_are_collected_not_dropped():
    sums = env.sum_by_team({"p1": line(rec_yd=900)}, {"p1": ""})
    assert "" in sums
    assert sums[""].get("rec_yd") == 900


# --------------------------------------------------------------------------- fit_bands


def test_bands_are_observed_extremes_widened_by_measured_drift(bandset):
    band = bandset.bands["pass_yd"]
    assert band.observed_min == 3600
    assert band.observed_max == 4600
    assert band.median == pytest.approx(4200)  # mean of the two middle values
    assert band.n_team_seasons == 4
    assert band.drift_measured is True
    # drift on pass_yd is 3200/4000 - 1 = -20% and 4400/4000 - 1 = +10%
    assert band.drift_low == pytest.approx(-0.20)
    assert band.drift_high == pytest.approx(0.10)
    assert band.low == pytest.approx(3600 * 0.80)
    assert band.high == pytest.approx(4600 * 1.10)


def test_drift_for_non_yardage_stats_is_flagged_as_a_transported_proxy(bandset):
    """The cached weekly history has no attempts/targets/TD columns, so their widening cannot
    be measured. That has to be visible on the Band, not buried."""
    for stat in ("pass_att", "rush_att", "rec", "rec_tgt", "total_td"):
        band = bandset.bands[stat]
        assert band.drift_measured is False, stat
        assert "PROXY" in band.drift_note or "proxy" in band.drift_note
    # the proxy is the widest MEASURED drift, not an invented number
    assert bandset.bands["pass_att"].drift_low == pytest.approx(-0.20)
    assert bandset.bands["pass_att"].drift_high == pytest.approx(0.10)


def test_band_provenance_records_what_was_fitted(bandset):
    prov = bandset.provenance
    assert prov["fit_season"] == 2025
    assert prov["n_team_seasons"] == 4
    assert prov["drift_seasons"] == (2024, 2025, 2026)
    assert set(prov["drift_measured_stats"]) == {"pass_yd", "rush_yd", "rec_yd"}


def test_fit_bands_refuses_an_empty_sample():
    with pytest.raises(ValueError):
        env.fit_bands(team_actuals={}, yardage_means=FAKE_YARDAGE_MEANS, fit_season=2025)


# --------------------------------------------------------------------------- check_bands


def _team(stats: dict[str, float]) -> dict[str, env.TeamSum]:
    """One synthetic team's sums, with total_td derived the same way sum_by_team does."""
    full = {s: 0.0 for s in env.TEAM_SUM_STATS}
    full.update(stats)
    full["total_td"] = full["pass_td"] + full["rush_td"]
    return {"XXX": env.TeamSum(team="XXX", n_players=14, stats=full, contributors={})}


def test_an_obviously_impossible_team_is_flagged(bandset):
    """The headline case from the plan: an offense projected 700 targets. The band's high is
    580 * 1.10 = 638, so 700 must come back as an OVER violation."""
    sums = _team({"rec_tgt": 700.0})
    violations = env.check_bands(sums, bandset)
    over = [v for v in violations if v.stat == "rec_tgt" and v.direction == "over"]
    assert len(over) == 1
    assert over[0].value == 700
    assert over[0].band.high == pytest.approx(580 * 1.10)
    assert over[0].excess == pytest.approx(700 - 580 * 1.10)
    assert over[0].is_violation is True


def test_a_plausible_team_is_not_flagged(bandset):
    """Everything set to the fitted median must produce no violation at all -- otherwise the
    check flags all 32 teams and means nothing."""
    sums = _team(
        {
            "pass_att": bandset.bands["pass_att"].median,
            "pass_yd": bandset.bands["pass_yd"].median,
            "pass_td": 26.0,
            "rush_att": bandset.bands["rush_att"].median,
            "rush_yd": bandset.bands["rush_yd"].median,
            "rush_td": 15.0,
            "rec": bandset.bands["rec"].median,
            "rec_tgt": bandset.bands["rec_tgt"].median,
            "rec_yd": bandset.bands["rec_yd"].median,
            "rec_td": 26.0,
        }
    )
    assert env.check_bands(sums, bandset) == ()


def test_an_undershoot_is_reported_but_is_not_a_violation(bandset):
    """A partial roster ALWAYS undershoots, so an undershoot cannot be evidence of anything.
    It must still be visible -- silently dropping it would hide the coverage gap."""
    sums = _team({"rec_tgt": 100.0})
    violations = env.check_bands(sums, bandset)
    under = [v for v in violations if v.stat == "rec_tgt"]
    assert len(under) == 1
    assert under[0].direction == "under"
    assert under[0].is_violation is False
    assert env.check_bands(sums, bandset, include_under=False) == ()


def test_a_stat_the_source_does_not_publish_is_skipped_not_flagged(bandset):
    """Sleeper and FantasyPros publish no targets at all. A zero there is 'not published',
    never 'this offense will throw zero passes', so it must not become a violation."""
    sums = _team({"rec_tgt": 0.0, "pass_yd": bandset.bands["pass_yd"].median})
    assert [v.stat for v in env.check_bands(sums, bandset)] == []


def test_no_drift_mode_compares_against_the_raw_observed_extremes(bandset):
    """The widening is a transported proxy for most stats and is wide enough to swallow real
    overages, so the un-widened comparison has to be available."""
    sums = _team({"rec_tgt": 600.0})  # above observed max 580, below widened high 638
    assert env.check_bands(sums, bandset) == ()
    raw = env.check_bands(sums, bandset, use_drift=False)
    assert [(v.stat, v.direction) for v in raw] == [("rec_tgt", "over")]
    assert raw[0].band.observed_max == 580


# --------------------------------------------------------------------------- identities


def test_identity_tolerance_is_the_worst_deviation_in_the_real_actuals():
    """CCC is built with rec 392 against pass_cmp 390 and rec_yd 4420 against pass_yd 4400 --
    i.e. real aggregation noise. The tolerance must be measured off that, not chosen."""
    tol = env.fit_identity_tolerances(_with_derived(FAKE_ACTUALS))
    assert tol["completions_vs_receptions"] == pytest.approx(2 / 390)
    assert tol["pass_yards_vs_rec_yards"] == pytest.approx(20 / 4400)
    assert tol["pass_tds_vs_rec_tds"] == pytest.approx(0.0)


def test_receiving_side_above_the_passing_side_is_a_violation():
    """You cannot catch a pass nobody threw. This is the strongest check in the module and it
    needs no fitted band at all."""
    sums = _team({"pass_cmp": 350.0, "rec": 420.0, "pass_yd": 3800.0, "rec_yd": 4600.0})
    checks = env.check_identities(sums, {"completions_vs_receptions": 0.01,
                                        "pass_yards_vs_rec_yards": 0.01,
                                        "pass_tds_vs_rec_tds": 0.07})
    by_rule = {c.rule: c for c in checks}
    assert by_rule["completions_vs_receptions"].verdict == "overage"
    assert by_rule["completions_vs_receptions"].delta == pytest.approx(70)
    assert by_rule["completions_vs_receptions"].delta_pct == pytest.approx(0.2)
    assert by_rule["pass_yards_vs_rec_yards"].verdict == "overage"


def test_receiving_side_below_the_passing_side_is_only_a_shortfall():
    """This is what a missing 6th receiver looks like AND what an over-projected QB looks like.
    The check cannot tell them apart, so it must not claim a violation."""
    sums = _team({"pass_cmp": 400.0, "rec": 320.0})
    checks = env.check_identities(sums, {"completions_vs_receptions": 0.01})
    match = [c for c in checks if c.rule == "completions_vs_receptions"][0]
    assert match.verdict == "shortfall"
    assert match.is_violation is False


def test_a_deviation_inside_the_fitted_tolerance_is_ok():
    sums = _team({"pass_cmp": 400.0, "rec": 402.0})
    checks = env.check_identities(sums, {"completions_vs_receptions": 0.01})
    assert [c.verdict for c in checks if c.rule == "completions_vs_receptions"] == ["ok"]


def test_identities_skip_the_unattributable_bucket():
    sums = {"": env.TeamSum(team="", n_players=3,
                            stats={s: 0.0 for s in env.TEAM_SUM_STATS} | {"rec": 40.0},
                            contributors={})}
    assert env.check_identities(sums, {"completions_vs_receptions": 0.01}) == ()


# --------------------------------------------------------------------------- report / candidates


def test_build_report_and_rejection_candidates_name_the_source_and_stat(bandset):
    tol = env.fit_identity_tolerances(_with_derived(FAKE_ACTUALS))
    statlines = {
        "qb": line(pass_cmp=350, pass_yd=3800, pass_td=25),
        "wr": line(rec=420, rec_yd=4600, rec_td=25),
    }
    report = env.build_report(
        "made_up_source", statlines, {"qb": "XXX", "wr": "XXX"}, bandset, tol,
        n_dropped_unresolved=7,
    )
    assert report.source == "made_up_source"
    assert report.n_statlines == 2
    assert report.n_dropped_unresolved == 7
    assert report.identity_violations, "receiving side is 20% above the passing side"
    candidates = env.rejection_candidates([report])
    assert {(s, stat) for s, stat, _ in candidates} >= {("made_up_source", "rec")}
    assert env.COVERAGE_CAVEAT in report.caveats


# --------------------------------------------------------------------------- weekly history


def test_postseason_contaminated_history_fails_loudly(tmp_path):
    """Two weekly-history files sit in this repo's cache and only the newer one is filtered to
    the regular season. The CSV drops season_type, so contamination can be detected but never
    filtered out after the fact -- so it has to raise, not warn."""
    path = tmp_path / "contaminated.csv"
    path.write_text(
        "season,week,player_id,player_display_name,position,pass_yd,rush_yd,rec_yd\n"
        "2021,1,00-1,A,QB,300,0,0\n"
        "2021,21,00-1,A,QB,280,0,0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="POSTSEASON"):
        env.load_weekly_history_rows(path)


def test_league_yardage_means_divide_by_32(tmp_path):
    path = tmp_path / "clean.csv"
    path.write_text(
        "season,week,player_id,player_display_name,position,pass_yd,rush_yd,rec_yd\n"
        "2024,1,00-1,A,QB,3200,0,0\n"
        "2024,2,00-2,B,RB,0,640,0\n"
        "2025,1,00-1,A,QB,6400,0,NA\n",
        encoding="utf-8",
    )
    _, rows = env.load_weekly_history_rows(path)
    means = env.league_yardage_means(rows)
    assert means[2024]["pass_yd"] == pytest.approx(100.0)
    assert means[2024]["rush_yd"] == pytest.approx(20.0)
    assert means[2025]["pass_yd"] == pytest.approx(200.0)
    assert means[2025]["rec_yd"] == pytest.approx(0.0)  # "NA" is not a zero-yard game


# --------------------------------------------------------------------------- real cached data


@pytest.fixture(scope="module")
def real_actuals():
    from draftroom.prep.http import load_latest_raw

    try:
        raw = load_latest_raw("espn")
    except FileNotFoundError:
        pytest.skip("no cached ESPN payload under data/raw/espn/")
    return env.team_season_actuals(raw, 2025)


def test_real_2025_team_actuals_cover_all_32_teams(real_actuals):
    assert len(real_actuals) == 32


def test_real_2025_actuals_satisfy_the_accounting_identity(real_actuals):
    """The load-bearing evidence that the ESPN weekly aggregation is complete enough to fit on:
    on REAL data, where the identity is exact by definition, it closes to under 1% on every
    team. If this ever fails, the aggregation lost players and no band fitted from it is safe."""
    for team, stats in real_actuals.items():
        assert abs(stats["rec"] - stats["pass_cmp"]) / stats["pass_cmp"] < 0.01, team
        assert abs(stats["rec_yd"] - stats["pass_yd"]) / stats["pass_yd"] < 0.01, team


def test_real_2025_actuals_agree_with_the_independent_weekly_cache(real_actuals):
    """Two unrelated providers, same season: ESPN's team-mean yardage against nflreadpy's
    league total / 32. They agree to well under 1%, which is what makes the fit credible."""
    try:
        _, rows = env.load_weekly_history_rows()
    except (FileNotFoundError, ValueError):
        pytest.skip("no usable cached nflreadpy weekly history")
    means = env.league_yardage_means(rows)
    if 2025 not in means:
        pytest.skip("cached weekly history has no 2025")
    for stat in ("pass_yd", "rush_yd", "rec_yd"):
        espn_mean = sum(a[stat] for a in real_actuals.values()) / len(real_actuals)
        assert abs(espn_mean - means[2025][stat]) / means[2025][stat] < 0.02, stat
