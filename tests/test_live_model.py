"""Tests for the live-draft model: conditional survival, dynamic tiers, and VONA.

Two kinds of fixtures appear below. Conditioning and monotonicity properties are proven with
small synthetic mu/sd pairs, because those are properties of the math and shouldn't depend on
which season's ADP happens to be cached. Everything with a real-world claim attached --
"Josh Allen is gone by pick 9", "the QB cliff looks like this", "a run fires here and not
there" -- is computed from the actual cached FFC payload via
:func:`~draftroom.draft.survival.load_ffc_adp`, never invented, and printed so a wrong number
is visible rather than hidden behind a passing assertion.

No network, ever (CLAUDE.md): this only reads whatever is already cached under
``data/raw/ffc/``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pytest

from draftroom.draft.survival import (
    PositionalRun,
    expected_survivors,
    fit_sd_model,
    load_ffc_adp,
    p_available,
    survival,
    survival_curve,
    tier_exhaustion_pick,
)
from draftroom.draft.vona import expected_best_available, vona, vona_all_positions
from draftroom.tiers.dynamic import TierEngine, largest_gap_tiers

# --------------------------------------------------------------------------- shared fixtures


@pytest.fixture(scope="module")
def real_players():
    """The full cached FFC 2QB payload -- 222 rows verified in CLAUDE.md's anchor facts."""
    return load_ffc_adp()


@pytest.fixture(scope="module")
def real_fit(real_players):
    """sd ~ a + b*adp fit against the real cached payload (not hardcoded coefficients)."""
    return fit_sd_model(real_players)


@pytest.fixture(scope="module")
def real_qbs(real_players):
    return sorted((p for p in real_players if p.pos == "QB"), key=lambda p: p.adp)


@dataclass
class SimplePlayer:
    """Minimal player-ish record: whatever survival._mu_sd / _pos_of need, plus a pos field."""

    pos: str


@dataclass
class BoardPlayer:
    """A merged ADP + draft-value record -- what draft.vona and tiers.dynamic actually consume."""

    player_id: str
    pos: str
    adp: float
    stdev: float
    dv: float
    name: str = ""


# =============================================================================================
# 1. Conditioning
# =============================================================================================


class TestConditioning:
    def test_p_available_is_one_when_target_equals_current(self):
        assert p_available(mu=50.0, sd=8.0, target_pick=30, current_pick=30) == 1.0

    def test_p_available_is_one_when_target_is_before_current(self):
        # "Available now or sooner" is certain once he's demonstrably here.
        assert p_available(mu=50.0, sd=8.0, target_pick=10, current_pick=30) == 1.0

    def test_conditional_survival_never_smaller_than_unconditional(self):
        mu, sd = 40.0, 10.0
        for current in (1, 20, 39, 40):
            for target in (current, current + 1, current + 20, current + 60):
                cond = p_available(mu, sd, target, current)
                uncond = survival(mu, sd, target)
                assert cond >= uncond - 1e-12, (
                    f"conditional {cond} < unconditional {uncond} "
                    f"at current={current} target={target}"
                )

    def test_conditional_survival_is_monotone_decreasing_in_target_pick(self):
        mu, sd, current = 60.0, 12.0, 55
        picks = list(range(current, current + 80, 3))
        probs = [p_available(mu, sd, t, current) for t in picks]
        for a, b in zip(probs, probs[1:]):
            assert b <= a + 1e-12, f"survival rose from {a} to {b} as target increased"
        # And it isn't a trivial constant-1.0 curve -- it has to actually fall off.
        assert probs[-1] < probs[0]


# =============================================================================================
# 2. Real-data sanity: Josh Allen / Trevor Lawrence
# =============================================================================================


class TestRealDataSanity:
    def test_fit_sd_model_matches_the_verified_anchor_numbers(self, real_players, real_fit):
        """The FFC parse is sound, asserted on RELATIONSHIPS rather than on today's numbers.

        This test used to pin the live feed's actual values (Allen at ADP 1.5 with sd 0.7,
        Lawrence at 17.0, exactly 36 QBs). Those are all correct readings of one afternoon's
        payload and every one of them moved on the next refresh -- Allen to 1.7/0.9, Lawrence to
        19.3, 37 QBs -- which made a MANDATORY pre-draft data refresh look like a test failure.
        A test that reddens whenever the world changes trains you to ignore it, which is worse
        than not having it, because the failure it exists to catch (a swapped column, a units
        error, a truncated payload) hides among the noise.

        So it now asserts what a BROKEN PARSE could not satisfy, and prints the live figures so
        a wrong number is visible rather than hidden behind a passing assertion -- the same
        convention this module's docstring already sets.
        """
        allen = next(p for p in real_players if p.name == "Josh Allen")
        lawrence = next(p for p in real_players if p.name == "Trevor Lawrence")
        qbs = sorted((p for p in real_players if p.pos == "QB"), key=lambda p: p.adp)

        print(
            f"\nlive anchors: {len(real_players)} players, {len(qbs)} QBs | "
            f"Allen adp={allen.adp} sd={allen.stdev} | "
            f"Lawrence adp={lawrence.adp} sd={lawrence.stdev}"
        )
        print(f"fitted sd ~ adp: {real_fit.describe()}")

        # Allen is QB1 in this feed under every source (see FEEDBACK_LEDGER #1), and a top-of-
        # board player must carry a SMALL absolute stdev. A column swap or a units error is what
        # would break this, not a week of news.
        assert qbs[0].name == "Josh Allen", [p.name for p in qbs[:3]]
        assert 0.0 < allen.stdev < 2.0
        assert 0.0 < allen.adp < 5.0

        # Dispersion grows with ADP -- the single load-bearing property the survival model reads
        # out of this feed. Lawrence sits far enough down that both must exceed Allen's.
        assert lawrence.adp > allen.adp
        assert lawrence.stdev > allen.stdev
        assert real_fit.slope > 0.0, "sd must rise with adp or the survival model is inverted"

        # A 2QB feed puts many more QBs in range than a 1QB one. The band is wide on purpose: it
        # catches a truncated payload or a position mis-tag, and nothing else.
        assert 25 <= len(qbs) <= 60, f"{len(qbs)} QBs is not a plausible 2QB ADP feed"
        assert all(p.adp > 0 for p in real_players)
        assert real_fit.n == len(real_players)

        # THE COLUMN-SWAP GUARD. Every assertion above survives a swap on its own: Allen becomes
        # adp 0.9 / sd 1.7 and all the ordering still holds, so "Allen is QB1 with a small sd"
        # cannot tell the two columns apart. What separates them is SCALE. ADP is a pick number
        # across a ~180-pick board, so the deepest player must sit far down it; stdev is a
        # handful of picks wide even at the bottom.
        #
        # Verified by mutation, not asserted: swapping adp and stdev inside `load_ffc_adp` fails
        # this test. (A swap in `_mu_sd_of` does NOT, and should not be read as this test being
        # weak -- that function is not on the path `real_players` is built from.)
        adps = [p.adp for p in real_players]
        sds = [p.stdev for p in real_players if p.stdev is not None]
        print(f"adp range {min(adps)}-{max(adps)} | stdev range {min(sds)}-{max(sds)}")
        assert max(adps) > 100.0, (
            f"deepest ADP is only {max(adps)} across {len(real_players)} players -- that is not a "
            "draft board, and is what an adp/stdev column swap looks like"
        )
        assert max(sds) < max(adps) / 2.0, (
            f"stdev range ({max(sds)}) is implausibly wide against the ADP range ({max(adps)}) -- "
            "the two columns look exchanged"
        )

    def test_josh_allen_is_essentially_gone_by_pick_9(self, real_players, real_fit):
        allen = next(p for p in real_players if p.name == "Josh Allen")
        p9 = p_available(allen.adp, allen.stdev, target_pick=9, current_pick=1, fit=real_fit)
        print(f"\nJosh Allen (ADP {allen.adp}, sd {allen.stdev}) P(available at pick 9) = {p9:.6f}")
        assert p9 < 0.01

    def test_trevor_lawrence_survives_further_but_not_much_further(self, real_players, real_fit):
        lawrence = next(p for p in real_players if p.name == "Trevor Lawrence")
        p24 = p_available(lawrence.adp, lawrence.stdev, 24, current_pick=9, fit=real_fit)
        p33 = p_available(lawrence.adp, lawrence.stdev, 33, current_pick=9, fit=real_fit)
        print(
            f"\nTrevor Lawrence (ADP {lawrence.adp}, sd {lawrence.stdev}) "
            f"conditioned on available at pick 9: P(->24)={p24:.4f}  P(->33)={p33:.4f}"
        )
        # Materially non-trivial to 24 (real order of magnitude, not a rounding artifact)...
        assert p24 > 0.03
        # ...but poor to 33: an order of magnitude worse than the pick-24 number.
        assert p33 < 0.02
        assert p24 > 5 * p33


# =============================================================================================
# 3. QB cliff -- expected survivors at slot 9's own picks
# =============================================================================================


class TestQbCliff:
    SLOT_9_PICKS = (9, 16, 33, 40, 57, 64)

    def test_expected_qbs_remaining_is_monotone_decreasing(self, real_qbs, real_fit):
        curve = survival_curve(real_qbs, self.SLOT_9_PICKS, current_pick=1, fit=real_fit)
        print("\nExpected QBs remaining (all 36) at slot-9 picks:")
        for pk in self.SLOT_9_PICKS:
            print(f"  pick {pk:3d}: {curve[pk]:.2f}")
        values = [curve[pk] for pk in self.SLOT_9_PICKS]
        for a, b in zip(values, values[1:]):
            assert b <= a, "expected QB count did not decrease at the next pick"

    def test_expected_startable_qbs_remaining_is_monotone_decreasing(self, real_qbs, real_fit):
        """The top-24 (startable, per CLAUDE.md's 12x2QB demand) cut -- the decision-relevant one."""
        top24 = real_qbs[:24]
        curve = survival_curve(top24, self.SLOT_9_PICKS, current_pick=1, fit=real_fit)
        print("\nExpected STARTABLE (top-24) QBs remaining at slot-9 picks:")
        for pk in self.SLOT_9_PICKS:
            print(f"  pick {pk:3d}: {curve[pk]:.2f}")
        values = [curve[pk] for pk in self.SLOT_9_PICKS]
        for a, b in zip(values, values[1:]):
            assert b <= a
        # Sanity floor/ceiling: never more than the pool size, never negative.
        assert all(0 <= v <= 24 for v in values)


# =============================================================================================
# 4. Positional run detector
# =============================================================================================


class TestPositionalRun:
    def test_fires_on_a_genuine_run_in_a_thin_pool(self, real_players):
        """4 straight QB picks against a pool where QB is only ~13% of the top 30 -- a real run."""
        by_adp = sorted(real_players, key=lambda p: p.adp)
        thin_pool = by_adp[60:90]
        comp = Counter(p.pos for p in thin_pool)
        print(f"\nthin-pool composition (ranks 61-90 by ADP): {dict(comp)}")
        assert comp["QB"] / len(thin_pool) < 0.2, "fixture must actually be QB-thin"

        run = PositionalRun()
        readings = [run.observe("QB", thin_pool) for _ in range(4)]
        for i, r in enumerate(readings):
            print(f"  pick {i}: {r.describe()}")
        assert any(r.firing for r in readings), "a real positional run failed to fire"
        assert readings[-1].shift > 0.0

    def test_does_not_fire_when_position_merely_dominates_the_pool(self, real_players):
        """QB is 50% of the real top-30 remaining pool -- picking QB at that rate is the null
        hypothesis, not a run (CLAUDE.md: 'a QB-dense remaining pool means QB picks are
        expected'). Interleaved (not clustered) picks at the pool's own share must stay quiet.
        """
        by_adp = sorted(real_players, key=lambda p: p.adp)
        dominant_pool = by_adp[0:30]
        comp = Counter(p.pos for p in dominant_pool)
        print(f"\ndominant-pool composition (real top 30 by ADP): {dict(comp)}")
        assert comp["QB"] / len(dominant_pool) >= 0.4, "fixture must actually be QB-dominant"

        run = PositionalRun()
        sequence = ["QB", "RB", "QB", "WR", "QB", "RB", "QB", "WR", "QB", "RB"]
        readings = [run.observe(pos, dominant_pool) for pos in sequence]
        qb_readings = [r for pos, r in zip(sequence, readings) if pos == "QB"]
        for i, r in enumerate(qb_readings):
            print(f"  QB pick {i}: {r.describe()}")
        assert not any(r.firing for r in qb_readings), (
            "detector fired on QB picks that merely matched the pool's own 50% QB share"
        )

    def test_shift_decays_after_the_run_stops(self, real_players):
        by_adp = sorted(real_players, key=lambda p: p.adp)
        thin_pool = by_adp[60:90]
        run = PositionalRun()
        for _ in range(4):
            run.observe("QB", thin_pool)
        peak = run.shift("QB")
        print(f"\nQB shift right after the run: {peak:.3f}")
        assert peak > 0.0

        decaying = []
        for _ in range(6):
            run.observe("RB", thin_pool)
            decaying.append(run.shift("QB"))
        print(f"QB shift after each subsequent non-QB pick: {[round(s, 3) for s in decaying]}")
        assert decaying[0] < peak
        for a, b in zip(decaying, decaying[1:]):
            assert b <= a + 1e-9, "shift did not monotonically decay"
        assert decaying[-1] < peak * 0.2


# =============================================================================================
# 5. Tier stability
# =============================================================================================


class TestTierStability:
    @staticmethod
    def _qb_board(real_qbs) -> list[BoardPlayer]:
        # Draft-value proxy for tiering tests: monotone in ADP, real player identities/order.
        return [
            BoardPlayer(
                player_id=p.player_id, pos=p.pos, adp=p.adp, stdev=p.stdev,
                dv=max(0.0, 30.0 - p.adp / 3.0), name=p.name,
            )
            for p in real_qbs
        ]

    def test_a_pick_that_does_not_cross_a_tier_boundary_does_not_renumber(self, real_qbs):
        board = self._qb_board(real_qbs)
        engine = TierEngine()
        tiers_before = engine.update("QB", board)
        assert len(tiers_before) >= 2, "fixture should produce more than one tier to be meaningful"

        # Drop a player from the middle of the largest tier -- nowhere near a cliff boundary.
        biggest = max(tiers_before, key=lambda t: t.size)
        mid = biggest.members[len(biggest.members) // 2]
        print(f"\nremoving {mid.name!r} from the middle of tier {biggest.tier} (size {biggest.size})")
        remaining = [p for p in board if p.player_id != mid.player_id]

        tiers_after = engine.update("QB", remaining)

        label_before = {m.player_id: t.tier for t in tiers_before for m in t.members}
        label_after = {m.player_id: t.tier for t in tiers_after for m in t.members}
        moved = {
            pid: (label_before[pid], label_after[pid])
            for pid in label_before
            if pid in label_after and label_before[pid] != label_after[pid]
        }
        print(f"tier count before/after: {len(tiers_before)}/{len(tiers_after)}; relabeled: {moved}")
        assert not moved, f"players changed tier label with no boundary crossed: {moved}"


# =============================================================================================
# 6. VONA
# =============================================================================================


class TestVona:
    def test_sharp_cliff_position_yields_larger_vona_than_a_deep_position(self, real_players, real_fit):
        # Sharp-cliff board: value falls off steeply after the top few (elite-QB scarcity).
        qbs = [p for p in real_players if p.pos == "QB"]
        cliff_board = [
            BoardPlayer(
                player_id=p.player_id, pos="CLIFF", adp=p.adp, stdev=p.stdev,
                dv=max(0.0, 40.0 - p.adp * 0.6), name=p.name,
            )
            for p in qbs
        ]
        # Deep board: same player count and ADP structure, but draft value barely varies --
        # taking the "best" one now costs almost nothing versus waiting.
        deep_board = [
            BoardPlayer(
                player_id=p.player_id, pos="DEEP", adp=p.adp, stdev=p.stdev,
                dv=max(0.0, 20.0 - p.adp * 0.02), name=p.name,
            )
            for p in qbs
        ]

        current, nxt = 9, 33
        v_cliff = vona("CLIFF", cliff_board, current, nxt, fit=real_fit)
        v_deep = vona("DEEP", deep_board, current, nxt, fit=real_fit)
        print(f"\n{v_cliff.describe()}")
        print(v_deep.describe())

        assert v_cliff.vona > v_deep.vona
        assert v_deep.vona >= 0.0

    def test_vona_all_positions_covers_every_position_present(self, real_players, real_fit):
        board = [
            BoardPlayer(
                player_id=p.player_id, pos=p.pos, adp=p.adp, stdev=p.stdev,
                dv=max(0.0, 40.0 - p.adp * 0.3), name=p.name,
            )
            for p in real_players
            if p.pos in ("QB", "RB", "WR", "TE")
        ]
        result = vona_all_positions(board, current_pick=9, next_pick=16, fit=real_fit)
        assert set(result) == {"QB", "RB", "WR", "TE"}
        for pos, r in result.items():
            assert r.best_now >= r.expected_next - 1e-9, f"{pos}: VONA went negative"

    def test_expected_best_available_is_at_most_the_best_players_value(self, real_players, real_fit):
        qbs = [p for p in real_players if p.pos == "QB"]
        board = [
            BoardPlayer(
                player_id=p.player_id, pos=p.pos, adp=p.adp, stdev=p.stdev,
                dv=max(0.0, 40.0 - p.adp * 0.6), name=p.name,
            )
            for p in qbs
        ]
        best_dv = max(p.dv for p in board)
        e = expected_best_available(board, target_pick=33, current_pick=9, fit=real_fit)
        assert 0.0 <= e <= best_dv + 1e-9


# =============================================================================================
# 7. Tier exhaustion
# =============================================================================================


class TestTierExhaustion:
    def test_a_deep_tier_survives_a_short_horizon(self, real_qbs, real_fit):
        deep_tier = real_qbs[24:]  # QB25 onward -- 12 deep backups
        exh = tier_exhaustion_pick(deep_tier, current_pick=60, fit=real_fit, horizon=15)
        print(f"\ndeep backup-QB tier (n={len(deep_tier)}) exhaustion within 15 picks: {exh}")
        assert exh is None

    def test_a_thin_tier_returns_a_sensible_exhaustion_pick(self, real_qbs, real_fit):
        thin_tier = real_qbs[20:24]  # the last 4 of the top-24 startable tier
        exh = tier_exhaustion_pick(thin_tier, current_pick=60, fit=real_fit, horizon=40)
        print(
            f"\nthin QB tier {[p.name for p in thin_tier]} "
            f"(ADPs {[round(p.adp, 1) for p in thin_tier]}) exhaustion pick: {exh}"
        )
        assert exh is not None
        assert 60 < exh <= 100
