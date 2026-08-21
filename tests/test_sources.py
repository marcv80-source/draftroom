"""Tests for the per-source pool layer (plan 2026-08-20, B2) and its live_data plumbing.

`draftroom.sources` is the seam the server's projection-source toggle imports defensively, so
the contract tested here is deliberately narrow and exact: the key list, the two function
signatures, the caching, and -- most importantly -- that nothing in it raises at startup.

Real cached data only, no network (CLAUDE.md).
"""

from __future__ import annotations

import pytest

from draftroom import live_data, sources
from draftroom.validate import board as board_mod


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Every test starts from a cold cache and leaves one behind, so a test that deliberately
    breaks a source cannot poison the next one."""
    sources.clear_cache()
    yield
    sources.clear_cache()


# ------------------------------------------------------------------------ the key contract


def test_source_keys_match_the_boards_own_key_list():
    """`sources.SOURCE_KEYS` is a literal (so importing this module never drags in the
    valuation pipeline just to enumerate keys). This test is what keeps the duplicate honest."""
    assert sources.SOURCE_KEYS == board_mod.BOARD_SOURCE_KEYS


def test_default_source_agrees_everywhere():
    assert live_data.DEFAULT_SOURCE == board_mod.DEFAULT_BOARD_SOURCE
    assert sources.DEFAULT_SOURCE == board_mod.DEFAULT_BOARD_SOURCE
    assert sources.DEFAULT_SOURCE == "blend"


def test_every_key_has_a_label_and_a_note():
    for key in sources.SOURCE_KEYS:
        assert sources.SOURCE_LABELS[key]
        assert sources.SOURCE_NOTES[key]


def test_espn_label_does_not_pretend_clay_is_a_separate_source():
    """CLAUDE.md verified ESPN's API and Mike Clay's PDF are ONE source, 411/411 identical."""
    assert "clay" not in {k.lower() for k in sources.SOURCE_KEYS}
    assert "Clay" in sources.SOURCE_LABELS["espn"]


def test_the_blend_label_states_how_many_families_it_averages():
    """The header control is the only place Marc sees what the default board is made of. A label
    saying "3-source" over a four-source blend is the kind of quiet drift that makes a number
    untrustworthy."""
    n = len(sources.SOURCE_KEYS) - 1  # every key except "blend" is a family
    assert sources.SOURCE_LABELS["blend"] == f"Blend ({n}-source)"


def test_the_disagreement_threshold_is_the_measured_80th_percentile():
    """The rule, enforced rather than documented: DISAGREEMENT_CV_THRESHOLD must be the 80th
    percentile of the CV distribution the real board actually produces. An absolute cutoff read
    off an older, narrower distribution silently changes what it flags -- the old 0.10 was the
    3-source p80 (19.9% of the board) and flags 29.3% against the 4-source distribution."""
    rb = board_mod.build_real_board()
    cvs = sorted(
        d.points_stdev / d.points_mean
        for d in rb.disagreement.values()
        if d.has_disagreement_signal and d.points_mean > 0
    )
    assert len(cvs) >= 180
    i = (len(cvs) - 1) * 0.80
    lo, hi = int(i), min(int(i) + 1, len(cvs) - 1)
    p80 = cvs[lo] + (cvs[hi] - cvs[lo]) * (i - lo)
    assert live_data.DISAGREEMENT_CV_THRESHOLD == pytest.approx(p80, abs=0.01), (
        f"threshold {live_data.DISAGREEMENT_CV_THRESHOLD} is no longer the measured p80 "
        f"({p80:.4f}) -- re-derive it and update the constant's docstring"
    )
    flagged = sum(1 for v in cvs if v >= live_data.DISAGREEMENT_CV_THRESHOLD)
    assert 0.15 <= flagged / len(cvs) <= 0.25, (
        f"flagging {flagged}/{len(cvs)} -- the badge is meant to mark the noisiest fifth"
    )


# ------------------------------------------------------------------------- pool_for_source


def test_pool_for_source_rejects_an_unknown_key():
    with pytest.raises(ValueError, match="unknown projection source"):
        sources.pool_for_source("clay")


def test_pool_for_source_is_cached_per_key():
    first = sources.pool_for_source("blend")
    second = sources.pool_for_source("blend")
    assert first is second, "a second call must not rebuild a 4-board-per-startup pipeline"


def test_every_source_builds_a_two_tier_pool_of_the_same_shape():
    """The two-tier guarantee (CLAUDE.md: "The player pool is TWO TIERS") must hold for every
    source, and the RANKED tier must be identical across sources -- switching the projection
    changes what a player is WORTH, never whether the board can see him."""
    ranked_ids: dict[str, set[str]] = {}
    for key in sources.SOURCE_KEYS:
        pool = sources.pool_for_source(key)
        ranked = [p for p in pool if p.is_ranked]
        unranked = [p for p in pool if not p.is_ranked]
        assert len(ranked) >= 180, f"{key}: only {len(ranked)} ranked"
        assert len(unranked) >= 500, f"{key}: only {len(unranked)} roster-only"
        assert all(p.value == 0.0 and not p.value_is_real for p in unranked)
        ranked_ids[key] = {p.player_id for p in ranked}
    base = ranked_ids["blend"]
    for key, ids in ranked_ids.items():
        assert ids == base, f"{key} lost or gained a ranked player relative to the blend"


def test_switching_source_changes_values_but_not_identities():
    sleeper = {p.player_id: p for p in sources.pool_for_source("sleeper") if p.is_ranked}
    blend = {p.player_id: p for p in sources.pool_for_source("blend") if p.is_ranked}
    assert set(sleeper) == set(blend)
    changed = [pid for pid in sleeper if abs(sleeper[pid].value - blend[pid].value) > 1e-9]
    assert len(changed) >= 100, (
        f"only {len(changed)} of {len(sleeper)} ranked players changed value between the "
        "Sleeper-only board and the blend -- the composite would be busywork if so"
    )


def test_a_ranked_player_the_active_source_cannot_value_keeps_his_name_and_no_projection():
    """FantasyPros covers fewer ranked players than Sleeper. The misses must survive in the
    pool with value 0.0 and value_is_real False -- bookkeeping first, per CLAUDE.md -- never be
    dropped and never be back-filled from another source."""
    pool = sources.pool_for_source("fantasypros")
    ranked = [p for p in pool if p.is_ranked]
    unvalued = [p for p in ranked if not p.value_is_real]
    assert unvalued, "expected at least one ranked player FantasyPros does not cover"
    for p in unvalued:
        assert p.name
        assert p.value == 0.0
        assert p.value_sd == 0.0


# ---------------------------------------------------------------------- available_sources


def test_available_sources_reports_every_key_with_real_counts():
    entries = sources.available_sources()
    assert [e["key"] for e in entries] == list(sources.SOURCE_KEYS)
    for e in entries:
        assert e["label"] and e["note"]
        assert e["available"] is True, f"{e['key']} unavailable: {e['note']}"
        assert e["player_count"] >= 180
        assert e["total_count"] > e["player_count"]


def test_available_sources_is_cached_and_returns_copies():
    first = sources.available_sources()
    second = sources.available_sources()
    assert first == second
    assert first is not second, "callers must not be able to mutate the cache"
    first[0]["player_count"] = -1
    assert sources.available_sources()[0]["player_count"] >= 180


def test_available_sources_never_raises_when_a_source_cannot_build(monkeypatch):
    """It runs at server startup. A broken source must degrade to an entry saying so, because a
    header control that fails to render is worse than a source listed as unavailable."""
    real = live_data.load_player_pool

    def flaky(*args, **kwargs):
        if kwargs.get("source") == "espn":
            raise FileNotFoundError("no cached ESPN payload")
        return real(*args, **kwargs)

    monkeypatch.setattr(live_data, "load_player_pool", flaky)
    entries = {e["key"]: e for e in sources.available_sources()}
    assert entries["espn"]["available"] is False
    assert entries["espn"]["player_count"] == 0
    assert "UNAVAILABLE" in entries["espn"]["note"]
    assert "no cached ESPN payload" in entries["espn"]["note"]
    # ...and every other source is unaffected.
    assert entries["blend"]["available"] is True
    assert entries["sleeper"]["available"] is True


def test_available_sources_flags_a_fallback_mode_pool_as_unavailable(monkeypatch):
    """A pool that built but valued nobody is the ADP-placeholder fallback. It must NOT be
    offered as a working projection source -- recommendations from it are not the model."""
    monkeypatch.setattr(live_data, "_load_real_board_by_key", lambda source="blend": {})
    entries = {e["key"]: e for e in sources.available_sources()}
    for key in sources.SOURCE_KEYS:
        assert entries[key]["available"] is False
        assert "UNAVAILABLE" in entries[key]["note"]


# -------------------------------------------------------------------- live_data plumbing


def test_load_player_pool_takes_a_source_and_still_works_with_no_arguments():
    """server.py calls `load_player_pool()` bare, and tests monkeypatch it with a zero-arg
    lambda -- the default must stay callable with no arguments."""
    pool = live_data.load_player_pool()
    assert pool
    assert any(p.is_ranked for p in pool)


def test_load_player_pool_rejects_nothing_silently_for_a_bad_source(caplog):
    """A bad key reaches build_real_board, which raises; live_data degrades to FALLBACK mode
    and says so loudly rather than serving a different source's numbers under the wrong label."""
    with caplog.at_level("WARNING"):
        pool = live_data.load_player_pool(source="clay")
    assert any("REAL BOARD UNAVAILABLE" in r.message for r in caplog.records)
    assert all(not p.value_is_real for p in pool)


def test_value_by_source_carries_season_points_for_every_key():
    pool = sources.pool_for_source("blend")
    expected = set(board_mod.BOARD_SOURCE_KEYS)  # the four families plus "blend"
    valued = [p for p in pool if p.is_ranked and p.value_is_real and p.value_by_source]
    assert len(valued) >= 150
    full = [p for p in valued if set(p.value_by_source or {}) == expected]
    assert len(full) >= 150, (
        f"only {len(full)} players carried all {len(expected)} keys "
        "(the four families plus the blend)"
    )
    # Season points, not DraftValue: the best player on the board scores HUNDREDS of season
    # points and carries a dv in the tens. Checking the top of the board rather than an
    # arbitrary row keeps this a scale tripwire and not a claim about a deep bench player, whose
    # honest per-source projections legitimately run 20-50 season points.
    best = max(full, key=lambda p: p.value)
    vbs = best.value_by_source or {}
    assert all(v > 100.0 for v in vbs.values()), (best.name, vbs)
    assert vbs["blend"] != pytest.approx(vbs["sleeper"], abs=1e-9) or len(set(vbs.values())) == 1


def test_value_by_source_omits_a_source_with_no_data_rather_than_zeroing_it():
    pool = sources.pool_for_source("blend")
    n_keys = len(board_mod.BOARD_SOURCE_KEYS)  # the four families plus the blend
    partial = [
        p
        for p in pool
        if p.is_ranked and p.value_by_source and len(p.value_by_source) < n_keys
    ]
    assert partial, "expected at least one ranked player missing a source"
    for p in partial:
        assert all(v > 0.0 for v in (p.value_by_source or {}).values()), (
            "a missing source must be ABSENT from value_by_source, never present at 0.0"
        )


def test_unranked_players_carry_no_value_by_source():
    pool = sources.pool_for_source("blend")
    unranked = [p for p in pool if not p.is_ranked]
    assert unranked
    assert all(p.value_by_source is None for p in unranked)


def test_pool_player_can_still_be_constructed_positionally_through_value():
    """test_server.py builds PoolPlayer positionally up to `value`. New fields go at the END of
    the field list precisely so that keeps working."""
    p = live_data.PoolPlayer("qb1", "Josh Allen", "QB", "BUF", 7, 1.0, 0.7, 1, 200.0)
    assert p.is_ranked is True
    assert p.value_by_source is None
    assert p.value_is_real is False
