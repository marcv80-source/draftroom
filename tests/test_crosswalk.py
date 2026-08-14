"""Offline tests for the player-identity crosswalk.

Everything here runs against cached raw data already on disk under data/raw/
(sleeper, ffc, dynastyprocess) or against small synthetic fixtures built
in-process -- no network calls, per CLAUDE.md ("Never re-fetch in a test").
If data/raw/dynastyprocess is empty, run
`python -c "from draftroom.prep.crosswalk import fetch_dynastyprocess_csv as f; f()"`
once first (needs network).
"""

from __future__ import annotations

import csv

import pytest

from draftroom.prep.crosswalk import (
    DYNASTYPROCESS_SOURCE,
    OVERRIDES_PATH,
    _build_dynastyprocess_sleeper_index,
    _build_sleeper_cross_id_index,
    build_crosswalk,
    load_overrides,
)
from draftroom.prep.ffc_client import AdpRow, parse_adp_rows
from draftroom.prep.http import load_latest_raw
from draftroom.prep.sleeper_client import SKILL_POSITIONS

TOP_N_GATE = 200


# --------------------------------------------------------------------------- fixtures


def _sleeper_raw(*players: dict) -> dict:
    """Build a synthetic Sleeper raw-universe dict keyed by an incrementing pid."""
    return {str(i): p for i, p in enumerate(players, start=1)}


def _player(name: str, pos: str, team: str, active: bool = True, **cross_ids) -> dict:
    rec = {"full_name": name, "position": pos, "team": team, "active": active}
    rec.update(cross_ids)
    return rec


def _ffc_row(name: str, pos: str, team: str, adp: float = 50.0, player_id: int | None = 999) -> AdpRow:
    return AdpRow(
        name=name, pos=pos, team=team, adp=adp, std_dev=1.0, high=1, low=100,
        times_drafted=10, bye=None, player_id=player_id,
    )


# --------------------------------------------------------------------------- stage 0: overrides


def test_load_overrides_creates_file_with_header_when_absent(tmp_path):
    path = tmp_path / "overrides.csv"
    assert not path.exists()
    result = load_overrides(path)
    assert result == {}
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("source,source_key,pid")
    assert "#" in text  # a comment row explaining the file


def test_load_overrides_parses_rows_and_skips_comments(tmp_path):
    path = tmp_path / "overrides.csv"
    path.write_text(
        "source,source_key,pid\n"
        "# a comment, with a comma in it\n"
        "ffc,123,7777\n"
        "\n"
        "yahoo,abc,8888\n",
        encoding="utf-8",
    )
    result = load_overrides(path)
    assert result == {("ffc", "123"): "7777", ("yahoo", "abc"): "8888"}


def test_override_wins_over_a_cascade_that_would_otherwise_resolve(tmp_path):
    """Even when name+team+pos would exactly match a different player, the
    override must win -- it's checked first, per the resolution order."""
    overrides_path = tmp_path / "overrides.csv"
    overrides_path.write_text(
        "source,source_key,pid\nffc,999,OVERRIDE_PID\n", encoding="utf-8"
    )
    raw = _sleeper_raw(_player("Marquise Brown", "WR", "KC"))
    cw = build_crosswalk(
        raw, [_ffc_row("Marquise Brown", "WR", "KC", player_id=999)],
        overrides_path=overrides_path,
    )
    entry = cw.entries[("ffc", "999")]
    assert entry.pid == "OVERRIDE_PID"
    assert entry.resolve_method == "override"
    assert cw.resolve("ffc", "999") == "OVERRIDE_PID"


def test_override_wins_even_when_automatic_cascade_would_be_unresolved(tmp_path):
    overrides_path = tmp_path / "overrides.csv"
    overrides_path.write_text(
        "source,source_key,pid\nffc,999,MANUAL_PID\n", encoding="utf-8"
    )
    raw = _sleeper_raw(_player("Someone Else", "WR", "KC"))
    cw = build_crosswalk(
        raw, [_ffc_row("Totally Unmatched Name", "WR", "ZZ", player_id=999)],
        overrides_path=overrides_path,
    )
    entry = cw.entries[("ffc", "999")]
    assert entry.pid == "MANUAL_PID"
    assert entry.resolve_method == "override"


# --------------------------------------------------------------------------- stage 1: direct ID


def test_direct_id_via_sleeper_cross_field_ignores_name_mismatch(tmp_path):
    """Direct ID equality must win even when the name looks nothing alike --
    that's the whole point of trusting the ID over the name."""
    raw = _sleeper_raw(_player("Correct Player", "WR", "KC", yahoo_id=555444))
    cw = build_crosswalk(raw, [], overrides_path=tmp_path / "overrides.csv")
    entry = cw.resolve_yahoo_row(
        "y1", name="Completely Different Name", team="ZZ", pos="WR", yahoo_id="555444"
    )
    assert entry.resolve_method == "direct_id"
    assert entry.pid == "1"
    assert "yahoo_id=555444" in entry.detail
    assert cw.resolve("yahoo", "y1") == "1"


def test_direct_id_via_dynastyprocess_pivot(tmp_path):
    """A source with no Sleeper-native cross-ID field (e.g. fantasypros_id)
    still resolves via the DynastyProcess sleeper_id pivot."""
    raw = _sleeper_raw(_player("Correct Player", "RB", "KC"))
    dp_csv = (
        "sleeper_id,fantasypros_id,name,position,team\n"
        "1,fp-9999,Correct Player,RB,KC\n"
    )
    cw = build_crosswalk(
        raw, [], dynastyprocess_csv_text=dp_csv, overrides_path=tmp_path / "overrides.csv"
    )
    entry = cw.resolve_fantasypros_row(
        "fp1", name="Nothing Alike", team="ZZ", pos="RB", fantasypros_id="fp-9999"
    )
    assert entry.resolve_method == "direct_id"
    assert entry.pid == "1"
    assert "dynastyprocess" in entry.detail


def test_direct_id_ignores_dynastyprocess_zero_sentinel():
    """DynastyProcess uses '0' as a missing-value sentinel in some columns
    (confirmed on stats_global_id); '0' must never be treated as a real ID."""
    dp_csv = (
        "sleeper_id,stats_global_id,name,position,team\n"
        "1,0,Player A,WR,KC\n"
        "2,0,Player B,WR,SF\n"
    )
    index = _build_dynastyprocess_sleeper_index(dp_csv)
    assert index["stats_global_id"] == {}


def test_dynastyprocess_index_logs_but_keeps_first_on_id_collision(caplog):
    dp_csv = (
        "sleeper_id,pff_id,name,position,team\n"
        "1,DUPID,Player A,WR,KC\n"
        "2,DUPID,Player B,WR,SF\n"
    )
    with caplog.at_level("WARNING"):
        index = _build_dynastyprocess_sleeper_index(dp_csv)
    assert index["pff_id"]["DUPID"] == "1"  # first seen wins
    assert any("multiple sleeper_ids" in r.message for r in caplog.records)


def test_sleeper_cross_id_index_skips_na_and_empty_values():
    raw = _sleeper_raw(_player("Player A", "WR", "KC", espn_id=None, yahoo_id="", rotowire_id="NA"))
    index = _build_sleeper_cross_id_index(raw)
    assert index["espn_id"] == {}
    assert index["yahoo_id"] == {}
    assert index["rotowire_id"] == {}


# --------------------------------------------------------------------------- stage 2/3: exact name matches


def test_exact_name_team_pos_match():
    raw = _sleeper_raw(_player("Justin Jefferson", "WR", "MIN"))
    cw = build_crosswalk(raw, [_ffc_row("Justin Jefferson", "WR", "MIN")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "exact_name_team_pos"
    assert entry.pid == "1"


def test_exact_name_pos_ignores_team_mismatch_from_a_trade():
    """A player traded since the last source refresh: FFC says one team,
    Sleeper says another, but the name+pos is unique -- must still resolve."""
    raw = _sleeper_raw(_player("Stefon Diggs", "WR", "NE"))
    cw = build_crosswalk(raw, [_ffc_row("Stefon Diggs", "WR", "HOU")])  # stale team on FFC's side
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "exact_name_pos"
    assert entry.pid == "1"


def test_exact_name_team_pos_tie_goes_unresolved_not_guessed():
    """Two Sleeper players share name+team+pos exactly -- never pick one."""
    raw = _sleeper_raw(
        _player("Duplicate Name", "WR", "KC"),
        _player("Duplicate Name", "WR", "KC"),
    )
    cw = build_crosswalk(raw, [_ffc_row("Duplicate Name", "WR", "KC")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "unresolved"
    assert entry.pid is None
    assert "ambiguous" in entry.detail


def test_exact_name_pos_tie_across_teams_goes_unresolved():
    """Two Sleeper players share name+pos but differ by team from each other
    AND from the FFC row's team -- stage 3 must not guess between them."""
    raw = _sleeper_raw(
        _player("Duplicate Name", "WR", "KC"),
        _player("Duplicate Name", "WR", "SF"),
    )
    cw = build_crosswalk(raw, [_ffc_row("Duplicate Name", "WR", "ZZ")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "unresolved"
    assert entry.pid is None
    assert "ambiguous" in entry.detail


# --------------------------------------------------------------------------- stage 4: fuzzy


def test_fuzzy_match_resolves_when_uniquely_above_threshold_and_margin():
    """'Jonathon Taylor' (typo) vs Sleeper's 'Jonathan Taylor': token_sort_ratio
    ~93.3, well above the 90 threshold, and no other RB is close."""
    raw = _sleeper_raw(
        _player("Jonathan Taylor", "RB", "IND"),
        _player("Someone Unrelated", "RB", "SF"),
    )
    cw = build_crosswalk(raw, [_ffc_row("Jonathon Taylor", "RB", "IND")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "fuzzy"
    assert entry.pid == "1"
    assert "score=" in entry.detail


def test_fuzzy_match_respects_position_boundary():
    """A same-name-adjacent player at a different position must never be a
    fuzzy candidate -- fuzzy search is scoped to same-position only."""
    raw = _sleeper_raw(_player("Jonathan Taylor", "QB", "IND"))  # wrong position
    cw = build_crosswalk(raw, [_ffc_row("Jonathon Taylor", "RB", "IND")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "unresolved"
    assert entry.pid is None


def test_fuzzy_tie_below_margin_goes_unresolved():
    """'Jordan Davis' query against 'Jordan Davies' (96.0) and 'Jordyn Davis'
    (91.67): both above the 90 threshold, but only 4.3 apart -- below the
    required 5-point margin, so this must NOT guess."""
    raw = _sleeper_raw(
        _player("Jordan Davies", "RB", "KC"),
        _player("Jordyn Davis", "RB", "SF"),
    )
    cw = build_crosswalk(raw, [_ffc_row("Jordan Davis", "RB", "ZZ")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "unresolved"
    assert entry.pid is None
    assert "fuzzy candidates" in entry.detail


def test_fuzzy_match_below_threshold_goes_unresolved():
    raw = _sleeper_raw(_player("Completely Different Guy", "RB", "KC"))
    cw = build_crosswalk(raw, [_ffc_row("Nothing Alike Whatsoever", "RB", "ZZ")])
    entry = cw.entries[("ffc", "999")]
    assert entry.resolve_method == "unresolved"
    assert entry.pid is None


# --------------------------------------------------------------------------- out-of-league-scope (K/DST)


def test_def_and_pk_rows_are_out_of_scope_not_unresolved():
    """This league drafts no K/DST. A DEF/PK row must never be reported as a
    crosswalk miss -- it's a scope fact, distinct from a real unresolved name."""
    raw = _sleeper_raw(_player("Justin Jefferson", "WR", "MIN"))
    cw = build_crosswalk(
        raw,
        [
            _ffc_row("Denver Defense", "DEF", "DEN", adp=110.0, player_id=1001),
            _ffc_row("Brandon Aubrey", "PK", "DAL", adp=129.0, player_id=1002),
        ],
    )
    def_entry = cw.entries[("ffc", "1001")]
    pk_entry = cw.entries[("ffc", "1002")]
    assert def_entry.resolve_method == "out_of_league_scope"
    assert pk_entry.resolve_method == "out_of_league_scope"
    assert def_entry.pid is None and pk_entry.pid is None
    stats = cw.stats()
    assert stats["out_of_league_scope"] == 2
    assert "unresolved" not in stats


# --------------------------------------------------------------------------- Crosswalk.stats() / unresolved_report()


def test_stats_counts_by_resolve_method():
    raw = _sleeper_raw(
        _player("Justin Jefferson", "WR", "MIN"),
        _player("Stefon Diggs", "WR", "NE"),
    )
    cw = build_crosswalk(
        raw,
        [
            _ffc_row("Justin Jefferson", "WR", "MIN", player_id=1),
            _ffc_row("Stefon Diggs", "WR", "HOU", player_id=2),  # team-ignored match
            _ffc_row("Nobody At All", "WR", "ZZ", player_id=3),  # unresolved
        ],
    )
    stats = cw.stats()
    assert stats == {"exact_name_team_pos": 1, "exact_name_pos": 1, "unresolved": 1}


def test_unresolved_report_sorts_by_adp_ascending_and_includes_detail():
    raw = _sleeper_raw(_player("Justin Jefferson", "WR", "MIN"))
    cw = build_crosswalk(
        raw,
        [
            _ffc_row("Nobody One", "WR", "ZZ", adp=150.0, player_id=1),
            _ffc_row("Nobody Two", "WR", "ZZ", adp=20.0, player_id=2),
        ],
    )
    report = cw.unresolved_report()
    assert [r["name"] for r in report] == ["Nobody Two", "Nobody One"]
    assert all(r["detail"] for r in report)
    assert all(r["source"] == "ffc" for r in report)


# --------------------------------------------------------------------------- the real gate, offline against cached data


def test_top_200_completeness_gate_holds_against_cached_data():
    """The hard gate from CLAUDE.md: zero unresolved inside the top 200 FFC
    ADP players (restricted to positions this league actually drafts -- see
    resolve_cli.py). Runs entirely offline against whatever's cached under
    data/raw/."""
    try:
        sleeper_raw = load_latest_raw("sleeper")
        ffc_raw = load_latest_raw("ffc")
    except FileNotFoundError:
        pytest.skip("no cached raw data under data/raw/ -- run fetch_all first")

    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None

    ffc_rows = parse_adp_rows(ffc_raw)
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)

    relevant = [r for r in ffc_rows if (r.pos or "").strip().upper() in SKILL_POSITIONS]
    ranked = sorted(relevant, key=lambda r: r.adp)[:TOP_N_GATE]

    unresolved = []
    for row in ranked:
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        if cw.resolve("ffc", key) is None:
            unresolved.append(row.name)

    assert unresolved == [], f"top-{TOP_N_GATE} FFC players failed to resolve: {unresolved}"


def test_overrides_csv_committed_file_is_well_formed():
    """data/overrides.csv is committed to git (unlike everything else in
    data/) -- make sure it parses cleanly and every override actually
    resolves to a real player_id shape (non-empty string)."""
    if not OVERRIDES_PATH.exists():
        pytest.skip("data/overrides.csv not present in this checkout")
    overrides = load_overrides(OVERRIDES_PATH)
    for (source, source_key), pid in overrides.items():
        assert source and source_key and pid
