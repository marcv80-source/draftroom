"""Tests for the multi-source projection composite (plan 2026-08-20, B1).

Two kinds of test here, deliberately. The blending RULES are proven with tiny hand-built
statlines, because they are properties of the arithmetic and must not depend on which season's
projections happen to be cached. Everything with a real-world claim attached -- "ESPN is the
only source with targets", "the FantasyPros board has no per-player games figure" -- is computed
from the actual cached payloads under ``data/raw/`` and ``data/manual/``, never invented.

No network, ever (CLAUDE.md): every real-data test reads only what prep already cached.
"""

from __future__ import annotations

import pytest

from draftroom.config import LeagueConfig
from draftroom.prep import espn_client, fantasysharks_client, manual_csv, sleeper_client
from draftroom.prep.schema import CANONICAL_STATS, StatLine
from draftroom.validate import board as board_mod
from draftroom.valuation.composite import (
    COMPOSITE_SOURCES,
    SOURCE_PUBLISHES,
    SOURCE_PUBLISHES_BY_POS,
    blend_many,
    blend_statlines,
    games_distinct_counts,
    published_stats,
    varying_games_sources,
)
from draftroom.valuation.disagreement import INDEPENDENT_SOURCES

# --------------------------------------------------------------------- the publish tables


def test_publish_tables_are_derived_from_the_adapters_own_mappings():
    """Not a hand-copied list: each entry must equal what the adapter actually maps.

    A hand-maintained table would drift the first time an adapter learned a new field, and the
    drift would show up as a silently wrong denominator, not as an error.
    """
    assert SOURCE_PUBLISHES["sleeper"] == frozenset(sleeper_client.SLEEPER_STAT_MAP.values())
    assert SOURCE_PUBLISHES["espn"] == frozenset(espn_client.ESPN_STAT_ID_MAP.values())
    fp_union = frozenset(
        canonical
        for layout in manual_csv.POSITION_LAYOUTS.values()
        for _h, canonical in layout
        if canonical is not None
    )
    assert SOURCE_PUBLISHES["fantasypros"] == fp_union
    assert SOURCE_PUBLISHES["fantasysharks"] == fantasysharks_client.PUBLISHED_STATS
    assert SOURCE_PUBLISHES_BY_POS["fantasysharks"] == {
        pos: stats for pos, stats in fantasysharks_client.PUBLISHED_STATS_BY_POS.items()
    }


def test_the_composite_and_the_disagreement_measure_use_the_same_families():
    """Two names for the same set, on purpose (they answer different questions), so the only
    way they stay honest is a test. A family in one and not the other would mean the badge and
    the point estimate disagreed about what the board is made of."""
    assert set(COMPOSITE_SOURCES) == set(INDEPENDENT_SOURCES)
    assert len(COMPOSITE_SOURCES) == 4


def test_targets_come_from_exactly_two_of_the_four_sources():
    """Was the headline ASYMMETRY (ESPN alone); is now the headline WIN of adding FantasySharks.
    CLAUDE.md: Sleeper has no target field under any name (0 of 3,111 records) and FantasyPros
    publishes no targets column at all. FantasySharks publishes `Tgt` for RB/WR/TE, which took
    this stat from one source to two -- the specific reason the fourth family is worth more than
    a fourth vote (docs/FANTASYSHARKS.md)."""
    with_targets = {s for s in COMPOSITE_SOURCES if "rec_tgt" in SOURCE_PUBLISHES[s]}
    assert with_targets == {"espn", "fantasysharks"}
    # ...and it is NOT published for a quarterback by either of them being position-narrowed:
    # ESPN publishes one schema for all positions (and omits only real zeros), while
    # FantasySharks' QB table genuinely has no Tgt column.
    assert "rec_tgt" not in published_stats("fantasysharks", "QB")
    for pos in ("RB", "WR", "TE"):
        assert "rec_tgt" in published_stats("fantasysharks", pos)


def test_fantasypros_publishes_no_games_and_no_two_point_conversions():
    fp = SOURCE_PUBLISHES["fantasypros"]
    assert "games" not in fp
    for stat in ("pass_2pt", "rush_2pt", "rec_2pt"):
        assert stat not in fp
    # ...while both API sources do carry a games figure.
    assert "games" in SOURCE_PUBLISHES["sleeper"]
    assert "games" in SOURCE_PUBLISHES["espn"]


def test_fantasysharks_publishes_no_games_and_no_two_point_conversions():
    """Same two structural absences as FantasyPros, for the same reason: no column exists. The
    `games` half is what makes FantasySharks drop out of the games blend WITHOUT a special case
    -- see the varying_games_sources tests below."""
    fs = SOURCE_PUBLISHES["fantasysharks"]
    assert "games" not in fs
    for stat in ("pass_2pt", "rush_2pt", "rec_2pt"):
        assert stat not in fs
    for pos in ("QB", "RB", "WR", "TE"):
        assert "games" not in published_stats("fantasysharks", pos)


def test_fantasysharks_publish_set_is_position_dependent_on_rushing_attempts():
    """THE reason this source has to be position-keyed. `rush_att` is a real column for QB and
    RB and structurally absent for WR and TE (their tables carry rushing YARDS and TDs with no
    attempts column). Treated as a union, a tight end's structural-zero rush_att would divide
    Sleeper's and ESPN's real rushing attempts by three instead of two."""
    assert "rush_att" in published_stats("fantasysharks", "QB")
    assert "rush_att" in published_stats("fantasysharks", "RB")
    assert "rush_att" not in published_stats("fantasysharks", "WR")
    assert "rush_att" not in published_stats("fantasysharks", "TE")
    # ...but the yardage and TDs ARE published for WR/TE, so this is a missing COLUMN and not a
    # missing play type.
    for pos in ("WR", "TE"):
        assert {"rush_yd", "rush_td"} <= published_stats("fantasysharks", pos)
    # The union claims rush_att, which is exactly why the union is the coarse answer.
    assert "rush_att" in SOURCE_PUBLISHES["fantasysharks"]


def test_the_fantasysharks_publish_table_matches_the_real_cached_payload():
    """The verification, re-run rather than quoted: over the real cached pages, every declared
    stat must be nonzero on at least one row of its position (so the table is not too WIDE) and
    no undeclared stat may ever be nonzero (so it is not too NARROW). This is the same
    presence-vs-nonzero method the module docstring records for Sleeper and ESPN.

    The load-bearing measurement is rush_att: nonzero on 78 of 78 QB rows and 0 of 187 WR rows.
    """
    payload = fantasysharks_client.load_cached()
    pages = fantasysharks_client.pages_of(payload)
    nonzero_by_pos: dict[str, dict[str, int]] = {}
    for pos in ("QB", "RB", "WR", "TE"):
        rows = fantasysharks_client.parse_page(pages[pos], pos)
        assert rows, f"no {pos} rows parsed from the cached payload"
        counts = {stat: 0 for stat in CANONICAL_STATS}
        for row in rows:
            d = row.stats.as_dict()
            for stat in CANONICAL_STATS:
                if float(d.get(stat, 0.0)) != 0.0:
                    counts[stat] += 1
        declared = published_stats("fantasysharks", pos)
        seen = {s for s, n in counts.items() if n > 0}
        assert seen - declared == set(), (
            f"{pos}: nonzero but NOT declared published: {sorted(seen - declared)}"
        )
        assert declared - seen == set(), (
            f"{pos}: declared published but zero on every row: {sorted(declared - seen)}"
        )
        nonzero_by_pos[pos] = counts

    assert nonzero_by_pos["QB"]["rush_att"] > 0
    assert nonzero_by_pos["WR"]["rush_att"] == 0
    assert nonzero_by_pos["TE"]["rush_att"] == 0
    assert nonzero_by_pos["WR"]["rush_yd"] > 0, (
        "WR rushing yards are published; only the ATTEMPTS column is missing"
    )
    assert nonzero_by_pos["RB"]["rec_tgt"] > 0


def test_espn_publishes_every_canonical_stat():
    assert SOURCE_PUBLISHES["espn"] == frozenset(CANONICAL_STATS)


def test_fantasypros_publish_set_is_position_dependent():
    """A TE export has no rushing columns at all; a QB export has no receiving columns."""
    te = published_stats("fantasypros", "TE")
    qb = published_stats("fantasypros", "QB")
    assert "rush_yd" not in te and "rush_att" not in te
    assert "rec" in te
    assert "pass_yd" in qb and "rec" not in qb
    # The union is strictly wider than any single position's set -- which is exactly why
    # blend_statlines takes `pos`.
    assert te < SOURCE_PUBLISHES["fantasypros"]


def test_fantasysharks_contributes_nothing_to_games_by_measurement():
    """The wiring checklist's item 2, confirmed rather than assumed: FantasySharks must drop out
    of the games blend because ``varying_games_sources`` MEASURES 0 distinct positive values in
    its pool, not because anything hardcodes an exclusion. Run over the real resolved pool."""
    from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk
    from draftroom.prep.ffc_client import parse_adp_rows
    from draftroom.prep.http import load_latest_raw

    sleeper_raw = load_latest_raw("sleeper")
    ffc_rows = parse_adp_rows(load_latest_raw("ffc"))
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    resolved = board_mod._resolve_fantasysharks_statlines(cw)
    assert len(resolved) > 400, f"only {len(resolved)} FantasySharks rows resolved"

    counts = games_distinct_counts({"fantasysharks": resolved})
    assert counts["fantasysharks"] == 0
    assert varying_games_sources({"fantasysharks": resolved}) == frozenset()


def test_a_source_with_no_games_column_is_excluded_without_a_special_case():
    """The rule on fabricated pools, so it does not depend on this week's cache: a source whose
    published set has no `games` at all is reported as 0 distinct values and never admitted --
    the same mechanism that excludes FantasyPros, with no per-source branch anywhere."""
    counts = games_distinct_counts(
        {
            "fantasysharks": [StatLine(rec=90.0), StatLine(rec=40.0)],
            "espn": [StatLine(games=17.0), StatLine(games=11.0)],
        }
    )
    assert counts == {"fantasysharks": 0, "espn": 2}
    assert varying_games_sources(
        {"fantasysharks": [StatLine(rec=90.0)], "espn": [StatLine(games=17.0), StatLine(games=9.0)]}
    ) == frozenset({"espn"})


def test_api_sources_are_not_position_narrowed():
    """Sleeper and ESPN publish one schema for every position and omit only genuine zeros, so
    they are deliberately absent from the per-position table."""
    assert "sleeper" not in SOURCE_PUBLISHES_BY_POS
    assert "espn" not in SOURCE_PUBLISHES_BY_POS
    assert published_stats("sleeper", "TE") == SOURCE_PUBLISHES["sleeper"]
    assert published_stats("espn", "QB") == SOURCE_PUBLISHES["espn"]


def test_unknown_source_raises_rather_than_publishing_nothing():
    with pytest.raises(ValueError, match="unknown projection source"):
        published_stats("fantasypros_manual")  # the adapter's SOURCE constant, not a board key


# ------------------------------------------------------------------------ blending rules


def test_equal_weight_average_over_all_three_sources():
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(rush_yd=900.0, games=17.0),
            "espn": StatLine(rush_yd=1000.0, games=17.0),
            "fantasypros": StatLine(rush_yd=1100.0),
        },
        pos="RB",
    )
    assert blended.rush_yd == pytest.approx(1000.0)
    assert prov.n_by_stat["rush_yd"] == 3
    assert prov.sources_by_stat["rush_yd"] == ("espn", "fantasypros", "sleeper")
    assert prov.sources_present == ("espn", "fantasypros", "sleeper")


def test_a_stat_only_one_source_publishes_is_passed_through_not_divided():
    """THE trap this module exists for: ESPN's 172 targets must stay 172, not become 57."""
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(rec=120.0, rec_yd=1500.0, games=17.0),
            "espn": StatLine(rec=120.0, rec_tgt=172.0, rec_yd=1500.0, games=17.0),
            "fantasypros": StatLine(rec=120.0, rec_yd=1500.0),
        },
        pos="WR",
    )
    assert blended.rec_tgt == pytest.approx(172.0)
    assert prov.n_by_stat["rec_tgt"] == 1
    assert prov.sources_by_stat["rec_tgt"] == ("espn",)
    # ...and the stats all three DO publish are genuinely averaged over three.
    assert prov.n_by_stat["rec"] == 3


def test_a_missing_source_contributes_nothing():
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(rec_yd=1000.0, games=17.0),
            "espn": None,
            "fantasypros": StatLine(rec_yd=1200.0),
        },
        pos="WR",
    )
    assert blended.rec_yd == pytest.approx(1100.0)
    assert prov.sources_offered == ("fantasypros", "sleeper")
    assert prov.n_by_stat["rec_yd"] == 2
    # ESPN was the only source that could have supplied targets, and it is absent.
    assert blended.rec_tgt == 0.0
    assert prov.n_by_stat["rec_tgt"] == 0


def test_a_structural_zero_never_dilutes_another_sources_real_number():
    """A tight end with real rushing yards. FantasyPros' TE export has no rushing columns, so
    its structural 0.0 must not turn 100 into 66.7."""
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(rush_yd=100.0, games=17.0),
            "espn": StatLine(rush_yd=100.0, games=17.0),
            "fantasypros": StatLine(rec=80.0, rec_yd=900.0),
        },
        pos="TE",
    )
    assert blended.rush_yd == pytest.approx(100.0)
    assert prov.sources_by_stat["rush_yd"] == ("espn", "sleeper")


def test_omitting_pos_uses_the_wider_union_and_the_provenance_says_so():
    """Documents the cost of not passing `pos`: FantasyPros' union set includes rush_yd (its RB
    and WR exports have rushing columns), so a TE's structural zero WOULD dilute. The board
    always passes pos; this test exists so the difference is visible rather than folklore."""
    _, with_pos = blend_statlines(
        {
            "sleeper": StatLine(rush_yd=100.0),
            "fantasypros": StatLine(),
        },
        pos="TE",
    )
    blended_no_pos, without_pos = blend_statlines(
        {
            "sleeper": StatLine(rush_yd=100.0),
            "fantasypros": StatLine(),
        },
    )
    assert with_pos.n_by_stat["rush_yd"] == 1
    assert with_pos.pos == "TE"
    assert without_pos.n_by_stat["rush_yd"] == 2
    assert without_pos.pos is None
    assert blended_no_pos.rush_yd == pytest.approx(50.0)


def test_a_stat_no_source_published_is_zero_with_a_zero_count():
    blended, prov = blend_statlines({"fantasypros": StatLine(rec=80.0)}, pos="TE")
    assert blended.rec_tgt == 0.0
    assert prov.n_by_stat["rec_tgt"] == 0
    assert prov.sources_by_stat["rec_tgt"] == ()
    # A consensus zero is a DIFFERENT fact and is distinguishable by the count.
    zero_consensus, zprov = blend_statlines(
        {"sleeper": StatLine(rush_td=0.0), "espn": StatLine(rush_td=0.0)}, pos="WR"
    )
    assert zero_consensus.rush_td == 0.0
    assert zprov.n_by_stat["rush_td"] == 2


# ---------------------------------------------------------------------------------- games


def test_games_is_blended_only_over_sources_reporting_a_positive_figure():
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(games=18.0),
            "espn": StatLine(games=16.0),
            "fantasypros": StatLine(),  # publishes no games column at all
        },
        pos="RB",
    )
    assert blended.games == pytest.approx(17.0)
    assert prov.n_by_stat["games"] == 2
    assert prov.games_known is True


def test_a_zero_games_figure_is_never_averaged_in_as_zero_games_played():
    blended, prov = blend_statlines(
        {"sleeper": StatLine(games=17.0), "espn": StatLine(games=0.0)}, pos="QB"
    )
    assert blended.games == pytest.approx(17.0), "0.0 games means unknown, not 'played none'"
    assert prov.n_by_stat["games"] == 1


def test_games_unknown_everywhere_emits_zero_which_means_apply_the_prior():
    blended, prov = blend_statlines({"fantasypros": StatLine(rec=80.0)}, pos="TE")
    assert blended.games == 0.0
    assert prov.games_known is False
    assert prov.n_by_stat["games"] == 0


# ------------------------------------------------------------------------------ rejection


def test_rejecting_a_source_stat_pair_drops_it_from_that_stats_average_only():
    by_source = {
        "sleeper": StatLine(rec=100.0, rec_yd=1000.0),
        "espn": StatLine(rec=60.0, rec_yd=1400.0),
        "fantasypros": StatLine(rec=80.0, rec_yd=1200.0),
    }
    blended, prov = blend_statlines(by_source, rejected={("espn", "rec")}, pos="WR")
    assert blended.rec == pytest.approx(90.0), "average of sleeper+fantasypros only"
    assert prov.sources_by_stat["rec"] == ("fantasypros", "sleeper")
    # ESPN is still fine about everything else -- rejection is per (source, stat).
    assert blended.rec_yd == pytest.approx(1200.0)
    assert prov.sources_by_stat["rec_yd"] == ("espn", "fantasypros", "sleeper")
    assert prov.rejected_applied == (("espn", "rec"),)
    assert "espn" in prov.sources_present


def test_rejecting_every_source_for_a_stat_leaves_it_unknown_not_zero_by_consensus():
    blended, prov = blend_statlines(
        {"sleeper": StatLine(rec_td=8.0), "espn": StatLine(rec_td=10.0)},
        rejected={("sleeper", "rec_td"), ("espn", "rec_td")},
        pos="WR",
    )
    assert blended.rec_td == 0.0
    assert prov.n_by_stat["rec_td"] == 0
    assert set(prov.rejected_applied) == {("sleeper", "rec_td"), ("espn", "rec_td")}


def test_a_rejection_that_never_applied_is_not_reported_as_applied():
    """`rejected` is a Container, so the input cannot be enumerated -- provenance reports only
    what actually removed a contribution, for this player."""
    _, prov = blend_statlines(
        {"sleeper": StatLine(rec=100.0)},
        rejected={("espn", "rec"), ("sleeper", "rec_tgt")},
        pos="WR",
    )
    assert prov.rejected_applied == (), (
        "ESPN was absent and Sleeper never publishes rec_tgt, so neither rejection removed "
        "anything here"
    )


def test_rejected_accepts_any_container_including_a_plain_list():
    _, prov = blend_statlines(
        {"sleeper": StatLine(rec=100.0), "espn": StatLine(rec=60.0)},
        rejected=[("espn", "rec")],
        pos="WR",
    )
    assert prov.sources_by_stat["rec"] == ("sleeper",)


# -------------------------------------------------------------------------------- weights


def test_weights_are_applied_per_stat_and_normalised_over_who_contributed():
    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(rec_yd=1000.0, rec_tgt=0.0),
            "espn": StatLine(rec_yd=1400.0, rec_tgt=170.0),
        },
        weights={"sleeper": 1.0, "espn": 3.0},
        pos="WR",
    )
    assert blended.rec_yd == pytest.approx((1000.0 + 3 * 1400.0) / 4.0)
    # A single-source stat is unaffected by its weight -- normalisation is over contributors.
    assert blended.rec_tgt == pytest.approx(170.0)
    assert prov.weights == {"sleeper": 1.0, "espn": 3.0}


def test_a_zero_weight_source_contributes_nothing_and_is_absent_from_provenance():
    blended, prov = blend_statlines(
        {"sleeper": StatLine(rec_yd=1000.0), "espn": StatLine(rec_yd=1400.0)},
        weights={"sleeper": 0.0, "espn": 1.0},
        pos="WR",
    )
    assert blended.rec_yd == pytest.approx(1400.0)
    assert prov.sources_present == ("espn",)
    assert prov.sources_offered == ("espn", "sleeper"), "offered != contributed, on purpose"


def test_a_negative_weight_is_refused():
    with pytest.raises(ValueError, match="negative weight"):
        blend_statlines({"sleeper": StatLine()}, weights={"sleeper": -1.0})


def test_an_unknown_source_key_is_refused_not_ignored():
    with pytest.raises(ValueError, match="unknown projection source"):
        blend_statlines({"sleepr": StatLine(rec=10.0)})
    with pytest.raises(ValueError, match="unknown projection source"):
        blend_statlines({"sleeper": StatLine()}, weights={"clay": 1.0})


def test_the_blend_emits_component_stats_never_points():
    """CLAUDE.md: adapters emit component stats, never fantasy points. The composite blends the
    STATS and is scored once downstream -- so its output type is a StatLine, full stop."""
    blended, _ = blend_statlines({"sleeper": StatLine(pass_yd=4000.0)})
    assert isinstance(blended, StatLine)
    assert set(blended.as_dict()) == set(CANONICAL_STATS)


def test_provenance_describe_names_the_single_source_stats():
    _, prov = blend_statlines(
        {"sleeper": StatLine(rec=100.0, games=17.0), "espn": StatLine(rec=90.0, rec_tgt=140.0)},
        pos="WR",
    )
    text = prov.describe()
    assert "rec_tgt" in text
    assert "espn" in text and "sleeper" in text


# ------------------------------------------------------------------- against the real board


@pytest.fixture(scope="module")
def cfg():
    return LeagueConfig.from_yaml()


@pytest.fixture(scope="module")
def blend_board(cfg):
    return board_mod.build_real_board(cfg, source="blend")


def test_real_board_targets_now_come_from_two_sources(blend_board):
    """Measured on the real cached board, not asserted from the docs. This assertion INVERTED on
    2026-08-20: it used to require rec_tgt to have at most ONE contributor, because ESPN was the
    only source that published it. FantasySharks publishes it too, so most ranked players now
    get an AVERAGE of two real numbers -- which is the whole point of adding this family, and
    the thing the envelope validator needed in order to cross-check targets at all."""
    counts = {prov.n_by_stat["rec_tgt"] for prov in blend_board.blend_provenance.values()}
    assert counts <= {0, 1, 2}, f"rec_tgt has >2 contributors somewhere: {sorted(counts)}"
    contributors = {
        s
        for prov in blend_board.blend_provenance.values()
        for s in prov.sources_by_stat["rec_tgt"]
    }
    assert contributors <= {"espn", "fantasysharks"}
    two = sum(
        1 for prov in blend_board.blend_provenance.values() if prov.n_by_stat["rec_tgt"] == 2
    )
    any_targets = sum(
        1 for prov in blend_board.blend_provenance.values() if prov.n_by_stat["rec_tgt"] >= 1
    )
    assert any_targets > 100, f"only {any_targets} ranked players got targets at all"
    assert two > 100, (
        f"only {two} ranked players got targets from BOTH sources -- going 1 -> 2 on this stat "
        "is the specific justification for the fourth family"
    )


def test_real_board_most_players_blend_all_four_families(blend_board):
    four = sum(
        1
        for prov in blend_board.blend_provenance.values()
        if len(prov.sources_present) == len(COMPOSITE_SOURCES)
    )
    total = len(blend_board.blend_provenance)
    assert total >= 180
    assert four >= 150, f"only {four} of {total} ranked players had all four sources"


def test_real_board_no_blended_stat_was_rejected_yet(blend_board):
    """B6 is not wired: the equal-weight composite ships first, and the UI says so."""
    assert all(not prov.rejected_applied for prov in blend_board.blend_provenance.values())


def test_every_board_source_key_builds_and_reports_its_own_source(cfg):
    for key in board_mod.BOARD_SOURCE_KEYS:
        rb = board_mod.build_real_board(cfg, source=key)
        assert rb.source == key
        assert len(rb.players) >= 150, f"{key}: only {len(rb.players)} players"
        assert len(rb.seasons) == len(rb.players)


def test_an_empty_shell_projection_is_excluded_not_valued_at_zero(cfg):
    """Measured case, 2026-08-20: Sleeper's record for Ricky Pearsall is all-zero component
    stats with games=18.0 -- an empty projection, not a projection of zero production. The
    Sleeper-only board values him (ppg 0.0, a deeply negative dv) because its games gate admits
    him; the blend excludes him, because once Sleeper's constant games figure is out there is
    nothing left. Exclusion is the honest answer, and live_data still keeps the NAME for
    bookkeeping with value_is_real=False."""
    sleeper = board_mod.build_real_board(cfg, source="sleeper")
    blend = board_mod.build_real_board(cfg, source="blend")
    # ~0, not exactly 0: the bonus model contributes a rounding-scale 0.001 on an all-zero
    # stat line, which is itself a small illustration of why an empty shell should not be valued.
    empty = [s for s in sleeper.seasons if s.ppg < 0.01]
    assert empty, "expected at least one all-zero Sleeper projection in the cached data"
    blend_ids = {s.player_id for s in blend.seasons}
    for s in empty:
        assert s.player_id not in blend_ids, (
            f"{s.name} has no real projection from any source but the blend valued him anyway"
        )
    excluded_names = {r.name for r in blend.excluded}
    assert excluded_names, "the blend must record what it dropped, never silently drop it"


def test_board_refuses_an_unknown_source(cfg):
    with pytest.raises(ValueError, match="unknown board source"):
        board_mod.build_real_board(cfg, source="clay")


def test_board_source_keys_are_blend_plus_the_four_families():
    assert board_mod.BOARD_SOURCE_KEYS == ("blend", *COMPOSITE_SOURCES)
    assert board_mod.DEFAULT_BOARD_SOURCE == "blend"


def test_the_default_board_is_the_blend_not_sleeper(cfg):
    """The plan's headline correction: CLAUDE.md called ESPN the source of record while the code
    used Sleeper. Neither is -- the composite is."""
    default = board_mod.build_real_board(cfg)
    assert default.source == "blend"


@pytest.mark.parametrize("key", ["fantasypros", "fantasysharks"])
def test_a_no_games_column_board_leans_entirely_on_the_prior(cfg, key):
    """The plan flagged the FantasyPros board as the one most likely to trip something, and
    FantasySharks is now in exactly the same position: neither publishes a games column, so on
    either standalone board EVERY season must carry expected_games=None and take its volume from
    the fitted rank-conditional availability curve."""
    rb = board_mod.build_real_board(cfg, source=key)
    explicit = [s for s in rb.seasons if s.expected_games is not None]
    assert not explicit, f"{len(explicit)} {key} seasons carry an explicit games figure"


def test_single_source_boards_use_their_own_games_figure_unmodified(cfg):
    """A single-source board is that source, as it is -- including its games figure. Sleeper's
    blanket 18.0 is excluded from the BLEND (it carries no player-specific information), but the
    Sleeper-only board is not the place to correct Sleeper."""
    for key in ("sleeper", "espn"):
        rb = board_mod.build_real_board(cfg, source=key)
        explicit = [s for s in rb.seasons if s.expected_games is not None]
        assert len(explicit) == len(rb.seasons), f"{key}: {len(explicit)}/{len(rb.seasons)}"


def test_blend_games_comes_from_espn_alone_because_sleepers_is_a_constant(cfg):
    """The 2026-08-20 correction. Sleeper publishes ONE distinct games value (18.0) across all
    3,111 records; ESPN publishes seven. Blending 18.0 into ESPN's 11-game flag would give 14.5
    and the availability cap would then discard the only real per-player durability signal in
    the pipeline. So the blend's games figure must equal ESPN's exactly wherever ESPN resolved,
    and be absent (prior applies) wherever it did not."""
    blend = board_mod.build_real_board(cfg, source="blend")
    espn = board_mod.build_real_board(cfg, source="espn")
    espn_games = {s.player_id: s.expected_games for s in espn.seasons}

    admitted = {
        s
        for prov in blend.blend_provenance.values()
        for s in prov.sources_by_stat["games"]
    }
    assert admitted <= {"espn"}, f"a constant-games source leaked into the blend: {admitted}"
    assert any(
        "sleeper" in prov.games_excluded_as_constant
        for prov in blend.blend_provenance.values()
    ), "Sleeper's blanket games figure must be recorded as excluded, not silently dropped"

    with_games = [s for s in blend.seasons if s.expected_games is not None]
    without = [s for s in blend.seasons if s.expected_games is None]
    assert with_games and without, (
        f"{len(with_games)} with an explicit games figure, {len(without)} without -- expected "
        "both groups (ESPN covers most but not all of the ranked pool)"
    )
    for s in with_games:
        assert s.player_id in espn_games
    for s in without:
        assert s.player_id not in espn_games, (
            f"{s.name} has ESPN games but the blend dropped them"
        )


def test_a_constant_games_source_is_excluded_and_a_varying_one_admitted():
    """The rule itself, on fabricated pools, so it does not depend on this week's cache."""
    constant_pool = [StatLine(games=18.0) for _ in range(50)]
    varying_pool = [StatLine(games=float(g)) for g in (17, 17, 17, 11, 4)]
    no_games_pool = [StatLine(rec=80.0) for _ in range(10)]

    counts = games_distinct_counts(
        {"sleeper": constant_pool, "espn": varying_pool, "fantasypros": no_games_pool}
    )
    assert counts == {"sleeper": 1, "espn": 3, "fantasypros": 0}
    admitted = varying_games_sources(
        {"sleeper": constant_pool, "espn": varying_pool, "fantasypros": no_games_pool}
    )
    assert admitted == frozenset({"espn"})

    blended, prov = blend_statlines(
        {
            "sleeper": StatLine(pass_yd=4000.0, games=18.0),
            "espn": StatLine(pass_yd=4200.0, games=11.0),
        },
        pos="QB",
        games_sources=admitted,
    )
    assert blended.games == pytest.approx(11.0), "ESPN's real flag must survive intact"
    assert prov.sources_by_stat["games"] == ("espn",)
    assert prov.games_excluded_as_constant == ("sleeper",)
    # ...and excluding a source from `games` must not touch anything else it publishes.
    assert blended.pass_yd == pytest.approx(4100.0)
    assert prov.n_by_stat["pass_yd"] == 2


def test_a_source_becoming_varying_is_picked_up_with_no_code_change():
    """The rule is measured, not hardcoded: if Sleeper started publishing real per-player games
    tomorrow it would be admitted on the next build."""
    admitted = varying_games_sources(
        {"sleeper": [StatLine(games=17.0), StatLine(games=12.0), StatLine(games=17.0)]}
    )
    assert admitted == frozenset({"sleeper"})


def test_games_sources_none_keeps_the_permissive_rule_for_single_player_callers():
    blended, prov = blend_statlines(
        {"sleeper": StatLine(games=18.0), "espn": StatLine(games=16.0)}, pos="QB"
    )
    assert blended.games == pytest.approx(17.0)
    assert prov.games_excluded_as_constant == ()


def test_blend_many_derives_the_games_rule_itself():
    by_player = {
        "a": {"sleeper": StatLine(rec=80.0, games=18.0), "espn": StatLine(rec=70.0, games=17.0)},
        "b": {"sleeper": StatLine(rec=40.0, games=18.0), "espn": StatLine(rec=50.0, games=9.0)},
    }
    out = blend_many(by_player, pos_of={"a": "WR", "b": "WR"})
    assert out["b"][0].games == pytest.approx(9.0)
    assert out["a"][1].games_excluded_as_constant == ("sleeper",)


def test_points_by_source_carries_every_resolved_source_plus_the_blend(blend_board):
    assert blend_board.points_by_source, "no per-source points recorded"
    keys_seen: set[str] = set()
    for per in blend_board.points_by_source.values():
        keys_seen.update(per)
        assert "blend" in per, "the blend must always be quotable alongside the sources"
        for v in per.values():
            assert v == v  # not NaN
    assert keys_seen == {"blend", *COMPOSITE_SOURCES}


def test_points_by_source_never_fabricates_a_zero_for_a_missing_source(cfg):
    """A source with no data for a player must be ABSENT from the mapping, not present at 0.0 --
    a 0.0 would render as 'this source projects nothing', which is a claim nobody made."""
    espn_board = board_mod.build_real_board(cfg, source="espn")
    missing_espn = [
        pid for pid, per in espn_board.points_by_source.items() if "espn" not in per
    ]
    # Every player ON the espn board has espn points by construction; the interesting case is
    # the blend board, which includes players ESPN never covered.
    assert not missing_espn
    blend = board_mod.build_real_board(cfg, source="blend")
    n_keys = len(COMPOSITE_SOURCES) + 1  # the four families plus the blend itself
    partial = [per for per in blend.points_by_source.values() if len(per) < n_keys]
    assert partial, "expected at least one ranked player missing at least one source"
    for per in partial:
        assert all(v > 0 for v in per.values())


def test_the_blend_moves_the_board_relative_to_sleeper_only(cfg):
    """The reason B1 exists at all. Sleeper-only vs blend must differ in DRAFT-VALUE rank for a
    material number of players -- if it didn't, the composite would be busywork."""
    sleeper = board_mod.build_real_board(cfg, source="sleeper")
    blend = board_mod.build_real_board(cfg, source="blend")

    def ranks(rb):
        ordered = sorted(rb.players, key=lambda p: -p.dv)
        return {p.player_id: i for i, p in enumerate(ordered, start=1)}

    rs, rb_ = ranks(sleeper), ranks(blend)
    shared = set(rs) & set(rb_)
    moves = sorted(abs(rs[p] - rb_[p]) for p in shared)
    assert len(shared) >= 180
    assert moves[-1] >= 10, f"biggest draft-value rank move was only {moves[-1]}"
    assert sum(1 for m in moves if m > 5) >= 20


# ------------------------------------------------- the caveat that must survive a 4th source


def test_the_disagreement_caveat_names_four_families_and_keeps_its_substance():
    """The caveat is mandated copy that travels with the data (RealBoard.disagreement_caveat).
    Adding a family had to update the COUNT without softening the point: HIGH disagreement is
    the signal, LOW disagreement is NOT a safety signal, and more sources does not fix that --
    four forecasts of one season are not four independent looks at it."""
    from draftroom.valuation.disagreement import DISAGREEMENT_CAVEAT

    text = DISAGREEMENT_CAVEAT.lower()
    assert "four notionally independent source families" in text, (
        "the caveat must state the family count, and state the current one"
    )
    for source in INDEPENDENT_SOURCES:
        assert source in text, f"{source} is a family feeding the measure but is not named"
    # the substance, in both directions
    assert "not a safety signal" in text
    assert "danger signal" in text
    assert "wrong in the same" in text
    # ...and the ESPN/Clay lesson that gates any new family stays attached.
    assert "411/411" in DISAGREEMENT_CAVEAT
    assert "docs/fantasysharks.md" in text
    # The board carries it verbatim, not a paraphrase.
    assert board_mod.RealBoard.disagreement_caveat == DISAGREEMENT_CAVEAT
