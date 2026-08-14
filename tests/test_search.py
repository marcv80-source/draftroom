"""Tests for the draft-night type-ahead.

The thing being protected here is Marc's attention. He types three or four characters while looking at
the room, and the top match has to be the player he meant. Two failure modes matter:
  1. The right player isn't first  -> he has to stop and look, which is the whole cost we're removing.
  2. An already-drafted player is offered -> he records the same guy twice and the board goes wrong.
"""

from __future__ import annotations

import pytest

from draftroom.draft.search import SearchablePlayer, search


def P(pid, name, pos, team, rank):
    return SearchablePlayer(player_id=pid, name=name, pos=pos, team=team, overall_rank=rank)


# A pool with the collisions that actually bite: three Browns, two Johnsons, two Smiths,
# apostrophes, hyphens, suffixes, and a nickname.
POOL = [
    P("allen", "Josh Allen", "QB", "BUF", 1),
    P("gibbs", "Jahmyr Gibbs", "RB", "DET", 2),
    P("bijan", "Bijan Robinson", "RB", "ATL", 3),
    P("nacua", "Puka Nacua", "WR", "LAR", 4),
    P("maye", "Drake Maye", "QB", "NE", 5),
    P("chase", "Ja'Marr Chase", "WR", "CIN", 9),
    P("jsn", "Jaxon Smith-Njigba", "WR", "SEA", 12),
    P("stbrown", "Amon-Ra St. Brown", "WR", "DET", 18),
    P("jefferson", "Justin Jefferson", "WR", "MIN", 20),
    P("hbrown", "Hollywood Brown", "WR", "KC", 95),
    P("abrown", "A.J. Brown", "WR", "PHI", 25),
    P("dsmith", "DeVonta Smith", "WR", "PHI", 40),
    P("nsmith", "Nico Smith", "RB", "HOU", 180),
    P("djohnson", "Diontae Johnson", "WR", "CAR", 150),
    P("kjohnson", "Kendall Johnson", "RB", "NYJ", 60),
    P("pittman", "Michael Pittman Jr.", "WR", "IND", 55),
    P("jennings", "Jauan Jennings", "WR", "SF", 88),
]


def top(query, **kw):
    res = search(query, POOL, **kw)
    return res[0].player.player_id if res else None


def ids(query, **kw):
    return [m.player.player_id for m in search(query, POOL, **kw)]


# ------------------------------------------------------------------ core ranking


def test_ambiguous_last_name_returns_the_most_valuable_available_player():
    """Marc's stated example: typing 'johnson' should lead with the better Johnson."""
    result = ids("johnson")
    assert result[0] == "kjohnson"  # rank 60 beats rank 150
    assert "djohnson" in result


def test_three_browns_are_ordered_by_value_not_alphabet():
    """Three players share the surname; the list must lead with the best one still available."""
    result = ids("brown")
    assert result[0] == "stbrown"  # rank 18, the most valuable Brown
    assert result.index("stbrown") < result.index("abrown") < result.index("hbrown")


def test_short_prefix_is_enough():
    assert top("jeff") == "jefferson"
    assert top("nac") == "nacua"
    assert top("gib") == "gibbs"


def test_apostrophes_and_periods_are_ignored():
    """He is not going to type the apostrophe in Ja'Marr."""
    assert top("jamarr") == "chase"
    assert top("chase") == "chase"
    assert top("st brown") == "stbrown"
    assert top("stbrown") == "stbrown"


def test_hyphenated_name_matches_either_half():
    assert top("njigba") == "jsn"
    assert top("smithnjigba") == "jsn"


def test_generational_suffix_does_not_block_a_match():
    assert top("pittman") == "pittman"


def test_misspelling_still_finds_the_player():
    """Typing fast while looking at the board."""
    assert top("jefersn") == "jefferson"
    assert top("nacau") == "nacua"


def test_abbreviated_subsequence_works():
    assert top("jhmyr") == "gibbs"


def test_team_abbreviation_is_searchable_but_loses_to_a_name_match():
    assert "chase" in ids("cin")
    # 'buf' should not outrank a real name hit for a query that is clearly a name.
    assert top("allen") == "allen"


# ------------------------------------------------------------------ availability


def test_drafted_players_are_excluded_by_default():
    """The bug that would actively corrupt the board: offering someone already taken."""
    assert top("jeff") == "jefferson"
    assert "jefferson" not in ids("jeff", drafted={"jefferson"})


def test_search_falls_through_to_the_next_match_once_the_leader_is_gone():
    """Three Smiths. Take the best one and the list re-forms around who is actually left."""
    assert top("smith") == "dsmith"
    assert top("smith", drafted={"dsmith"}) == "nsmith"
    assert top("smith", drafted={"dsmith", "nsmith"}) == "jsn"


def test_no_remaining_match_returns_nothing_rather_than_a_wrong_player():
    """If the only matching player is drafted, silence beats offering someone he didn't type."""
    assert search("jeff", POOL, drafted={"jefferson"}) == []


def test_drafted_players_can_be_looked_up_explicitly():
    """The '~' lookup: 'wait, did someone already take him?'"""
    res = ids("jeff", drafted={"jefferson"}, include_drafted=True)
    assert "jefferson" in res


def test_ranking_reflects_who_is_left_not_a_static_list():
    """Once the better Johnson is gone, the other one leads. Value is availability-relative."""
    assert top("johnson") == "kjohnson"
    assert top("johnson", drafted={"kjohnson"}) == "djohnson"


# ------------------------------------------------------------------ guards


def test_empty_or_whitespace_query_returns_nothing():
    assert search("", POOL) == []
    assert search("   ", POOL) == []


def test_nonsense_query_returns_nothing_rather_than_a_bad_guess():
    """Silence is the correct answer -- it's what triggers the 'add unknown player' path."""
    assert search("zzzzqqq", POOL) == []


def test_limit_is_respected():
    assert len(search("a", POOL, limit=3)) <= 3


def test_value_prior_never_hijacks_a_clean_prefix_match():
    """The rank bonus must not let the #1 overall player steal someone else's name.

    'jenn' is unambiguously Jennings even though Josh Allen is rank 1 in the pool.
    """
    assert top("jenn") == "jennings"
    assert top("nico") == "nsmith"  # rank 180, but nobody else is 'nico'


@pytest.mark.parametrize("query", ["gibbs", "GIBBS", "  Gibbs  "])
def test_query_is_case_and_whitespace_insensitive(query):
    assert top(query) == "gibbs"
