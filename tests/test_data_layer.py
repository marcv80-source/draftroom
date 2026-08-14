"""Offline tests for the prep data-fetch layer.

These run entirely against the raw JSON already cached under data/raw/ by a
prior `python -m draftroom.prep.fetch_all` run (see CLAUDE.md: "Never re-fetch
in a test"). No test in this file makes a network call. If the cache is empty,
run fetch_all once first.
"""

from __future__ import annotations

import pytest

from draftroom.prep import ffc_client, sleeper_client
from draftroom.prep.fantasypros_client import NotConfiguredError, fetch_projections as fp_fetch_projections
from draftroom.prep.http import load_latest_raw
from draftroom.prep.schema import CANONICAL_STATS, StatLine, normalize_name


# --------------------------------------------------------------------------- name normalization


def test_normalize_name_lowercases_and_strips_punctuation():
    assert normalize_name("D'Andre Swift") == "dandre swift"
    assert normalize_name("Amon-Ra St. Brown") == "amon ra st brown"


def test_normalize_name_strips_generational_suffixes():
    assert normalize_name("Michael Pittman Jr.") == "michael pittman"
    assert normalize_name("Odell Beckham Jr.") == "odell beckham"
    assert normalize_name("Kenneth Walker III") == "kenneth walker"
    assert normalize_name("Kenneth Walker II") == "kenneth walker"


def test_normalize_name_collapses_whitespace():
    assert normalize_name("  Justin   Jefferson  ") == "justin jefferson"


def test_normalize_name_folds_nicknames_so_variants_match():
    assert normalize_name("Mike Pittman") == normalize_name("Michael Pittman")
    assert normalize_name("Ken Walker III") == normalize_name("Kenneth Walker")
    assert normalize_name("Josh Jacobs") == normalize_name("Joshua Jacobs")
    assert normalize_name("Josh Jacobs") == "joshua jacobs"
    # Folding only touches the first token, not surnames that happen to match a key.
    assert normalize_name("Will Fuller") == normalize_name("William Fuller")
    assert normalize_name("Chris Will") == "christopher will"


def test_normalize_name_known_football_aliases():
    assert normalize_name("Hollywood Brown") == normalize_name("Marquise Brown")
    assert normalize_name("Chig Okonkwo") == normalize_name("Chigoziem Okonkwo")
    assert normalize_name("Tank Dell") == normalize_name("Nathaniel Dell")
    assert normalize_name("Deebo Samuel") == normalize_name("Tyshun Samuel")
    assert normalize_name("Bam Knight") == normalize_name("Zonovan Knight")


def test_normalize_name_empty_input():
    assert normalize_name("") == ""


# --------------------------------------------------------------------------- canonical stat schema


def test_canonical_stats_matches_claude_md_vocabulary():
    assert CANONICAL_STATS == (
        "pass_att",
        "pass_cmp",
        "pass_yd",
        "pass_td",
        "pass_int",
        "pass_2pt",
        "rush_att",
        "rush_yd",
        "rush_td",
        "rush_2pt",
        "rec",
        "rec_tgt",
        "rec_yd",
        "rec_td",
        "rec_2pt",
        "fum_lost",
        "games",
    )


def test_statline_defaults_to_all_zero():
    sl = StatLine()
    assert sl.as_dict() == {name: 0.0 for name in CANONICAL_STATS}
    assert sl.has_nonzero_stats() is False


def test_statline_games_only_does_not_count_as_nonzero_stats():
    # `games` alone shouldn't make a player look like it has real production.
    sl = StatLine(games=17.0)
    assert sl.has_nonzero_stats() is False


# --------------------------------------------------------------------------- Sleeper mapping (offline, cached)


@pytest.fixture(scope="module")
def sleeper_projections_raw():
    return load_latest_raw("sleeper_projections")


@pytest.fixture(scope="module")
def sleeper_statlines(sleeper_projections_raw):
    return sleeper_client.to_statlines(sleeper_projections_raw)


def test_sleeper_projections_cache_is_a_list_of_records(sleeper_projections_raw):
    assert isinstance(sleeper_projections_raw, list)
    assert len(sleeper_projections_raw) > 0
    assert "stats" in sleeper_projections_raw[0]
    assert "player_id" in sleeper_projections_raw[0]


def test_sleeper_to_statlines_maps_mahomes_exactly(sleeper_statlines):
    # Patrick Mahomes, Sleeper player_id "4046" -- exact values captured from a
    # live cached pull on 2026-08-14. If Sleeper's numbers change, re-cache and
    # update this test; the point is that the MAPPING is exact, not the numbers.
    mahomes = sleeper_statlines.get("4046")
    assert mahomes is not None
    assert mahomes.pass_att == 555.0
    assert mahomes.pass_cmp == 370.0
    assert mahomes.pass_yd == 3962.0
    assert mahomes.pass_td == 29.0
    assert mahomes.pass_int == 12.0
    assert mahomes.pass_2pt == 1.0
    assert mahomes.rush_att == 47.0
    assert mahomes.rush_yd == 202.0
    assert mahomes.rush_td == 1.0
    assert mahomes.fum_lost == 2.0
    assert mahomes.games == 18.0
    # A QB with no receiving work should show zero receiving stats, not missing ones.
    assert mahomes.rec == 0.0
    assert mahomes.rec_yd == 0.0


def test_sleeper_to_statlines_maps_a_receiver_and_rusher(sleeper_statlines):
    # Jahmyr Gibbs, Sleeper player_id "9221".
    gibbs = sleeper_statlines.get("9221")
    assert gibbs is not None
    assert gibbs.rush_att == 255.0
    assert gibbs.rush_yd == 1251.0
    assert gibbs.rush_td == 12.0
    assert gibbs.rush_2pt == 1.0
    assert gibbs.rec == 63.0
    assert gibbs.rec_yd == 533.0
    assert gibbs.rec_td == 3.0
    assert gibbs.fum_lost == 1.0
    assert gibbs.games == 18.0
    assert gibbs.pass_att == 0.0


def test_sleeper_rec_tgt_is_always_zero_no_source_field(sleeper_statlines):
    # Confirmed live: Sleeper's season projections never include a targets
    # field under any name, so rec_tgt can never be populated from this source.
    assert all(sl.rec_tgt == 0.0 for sl in sleeper_statlines.values())


def test_sleeper_to_statlines_produces_some_nonzero_statlines(sleeper_statlines):
    nonzero = [sl for sl in sleeper_statlines.values() if sl.has_nonzero_stats()]
    assert len(nonzero) > 0


def test_sleeper_players_cache_filters_to_active_skill_positions():
    raw = load_latest_raw("sleeper")
    filtered = sleeper_client.filter_active_skill_players(raw)
    assert len(filtered) > 0
    assert len(filtered) < len(raw)  # filtering actually removed something
    for ref in filtered.values():
        assert ref.pos in {"QB", "RB", "WR", "TE"}
        assert ref.source == "sleeper"


# --------------------------------------------------------------------------- FFC parse (offline, cached)


@pytest.fixture(scope="module")
def ffc_raw():
    return load_latest_raw("ffc")


@pytest.fixture(scope="module")
def ffc_rows(ffc_raw):
    return ffc_client.parse_adp_rows(ffc_raw)


def test_ffc_raw_shape_has_players_list(ffc_raw):
    assert "players" in ffc_raw
    assert isinstance(ffc_raw["players"], list)
    assert len(ffc_raw["players"]) > 0


def test_ffc_parse_produces_adp_rows_with_std_dev(ffc_rows):
    assert len(ffc_rows) > 0
    row = ffc_rows[0]
    assert isinstance(row, ffc_client.AdpRow)
    assert row.std_dev is not None
    assert row.std_dev >= 0.0
    # every row should have a numeric std_dev, not just the first
    assert all(r.std_dev is not None for r in ffc_rows)


def test_ffc_is_really_2qb_format(ffc_rows):
    # This is the hard sanity gate from CLAUDE.md/spec: several QBs must land
    # in the top 20 overall picks for this to be a genuine 2QB ADP.
    passed, qbs_in_top20 = ffc_client.check_is_2qb_format(ffc_rows)
    assert passed is True
    assert len(qbs_in_top20) >= ffc_client.MIN_QBS_IN_TOP_20


def test_ffc_top_pick_is_plausible():
    # Loose smoke test that the field mapping (name/pos/adp) is wired correctly,
    # without hardcoding to a specific player who may not stay ADP #1 forever.
    raw = load_latest_raw("ffc")
    rows = ffc_client.parse_adp_rows(raw)
    top = min(rows, key=lambda r: r.adp)
    assert top.adp <= 3.0
    assert top.pos in {"QB", "RB", "WR", "TE"}
    assert top.name


# --------------------------------------------------------------------------- FantasyPros (no key configured)


def test_fantasypros_raises_clear_error_when_unconfigured():
    # No API key exists at %LOCALAPPDATA%\draftroom\secrets.json on this machine
    # as of writing this test. This must fail loudly and never hit the network.
    with pytest.raises(NotConfiguredError):
        fp_fetch_projections(position="QB")
