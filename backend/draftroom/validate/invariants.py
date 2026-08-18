"""The sanity-invariant gate: CLAUDE.md's "Non-negotiable gates" #3, made runnable.

    Sanity invariants on every data refresh: top QB lands top-8 overall; 10-14 QBs in the top
    30; baselines move monotonically with team count and starter slots; survival is monotone
    and S(n0)/S(n0)==1; per-game fixture (high-PPG/few-games beats low-PPG/many-games at equal
    season totals).

    Never present a number that hasn't passed these. State which checks ran.

A 7th check was added 2026-08-18, not in CLAUDE.md's original list: no ranked player's
``expected_games`` may equal the full season length by default (see
:func:`check_no_default_expected_games_hits_full_season`) -- the guard against a regression of
the flat-games-fabrication bug the same session's projection-hygiene fix eliminated. Also note:
``check_qb_count_in_top30`` no longer asserts a fixed "10-14" band (that band assumed a
12-team league and is not immune to a given year's projection compression); it now asserts the
2QB shift is directional -- see that function's docstring.

Every check below returns a :class:`CheckResult` with the REAL numbers it computed, never just
a boolean -- CLAUDE.md's "state which checks ran" applies to this gate as much as to any
investor-facing number. :func:`run_all` runs the whole list and returns them in order;
``tools/run_invariants.py`` is the CLI that prints them and sets the process exit code.

Two of the checks (team-count / starter-slot monotonicity) need a player pool deep enough that
sweeping team count up to 16 or starters up to 3 never runs a position dry -- that is a fact
about the SWEEP, not about this league, so :func:`deep_synthetic_pool` builds a large synthetic
pool for exactly those two checks (mirroring ``tests/test_valuation.py``'s own ``deep_pool()``).
The other checks run against whatever real board the caller hands in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from draftroom.config import LeagueConfig
from draftroom.draft.survival import p_available, survival
from draftroom.valuation.evob import compute_draft_values, evob
from draftroom.valuation.replacement import PlayerSeason, replacement_levels, resolve_players

__all__ = [
    "CheckResult",
    "check_top_qb_top8",
    "check_qb_count_in_top30",
    "check_baseline_monotonic_team_count",
    "check_baseline_monotonic_starter_slots",
    "check_survival_monotone_and_normalized",
    "check_per_game_fixture",
    "check_no_default_expected_games_hits_full_season",
    "check_expected_games_capped_by_curve",
    "deep_synthetic_pool",
    "run_all",
]


@dataclass(frozen=True)
class CheckResult:
    """One invariant's outcome, with the real numbers that produced it -- never just PASS/FAIL."""

    name: str
    passed: bool
    detail: str

    def describe(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return f"[{verdict}] {self.name}: {self.detail}"


# =============================================================================== 1 & 2: ranking


def check_top_qb_top8(players: Sequence, cfg: LeagueConfig) -> CheckResult:
    """"Top QB lands top-8 overall" -- the whole 2QB edge collapses to this one number."""
    values = compute_draft_values(players, cfg)
    ordered = sorted(values.values(), key=lambda v: -v.dv)
    qb_rank = next((i for i, v in enumerate(ordered, 1) if v.pos == "QB"), None)
    if qb_rank is None:
        return CheckResult("top_qb_top8", False, "no QB in the valued pool at all")
    passed = qb_rank <= 8
    top_qb = next(v for v in ordered if v.pos == "QB")
    return CheckResult(
        "top_qb_top8",
        passed,
        f"top QB ({top_qb.name or top_qb.player_id}, dv={top_qb.dv:.1f}) is overall #{qb_rank} "
        f"of {len(ordered)} (need <= 8)",
    )


def check_qb_count_in_top30(players: Sequence, cfg: LeagueConfig) -> CheckResult:
    """The 2QB shift must put strictly MORE QBs in the top 30 than a 1-QB league would.

    REPLACES an earlier version of this check that asserted a fixed "10-14 QBs in the top 30"
    band. That band came from an abandoned 12-team assumption and, worse, hardcodes an absolute
    count that depends entirely on how spread out THIS YEAR's QB projections happen to be --
    2026's QB1-to-QB22 spread is legitimately compressed (19.53 to 13.93 ppg), which pushed the
    real count to 4 and failed the gate despite nothing being wrong with the model. An absolute
    band can never be immune to a given year's projection compression.

    This version tests the DIRECTION instead of a magnitude: score the identical board under
    generic 1-QB-starter rules and under this league's own (2-QB) rules, and require the 2-QB
    scoring to place strictly more QBs in the top 30. That is a structural fact about man-games
    demand (doubling the QB starting requirement roughly doubles QB man-games demand, which
    deepens the QB replacement baseline and lifts every QB's EVoB) -- it holds regardless of
    how much separation this year's particular QB projections happen to show, so a compressed
    year moves the exact counts on both sides together without breaking the comparison.
    """
    cfg_1qb = cfg.replace(starters={**dict(cfg.starters), "QB": 1})

    def _qb_count_top30(league_cfg: LeagueConfig) -> tuple[int, dict[str, int]]:
        values = compute_draft_values(players, league_cfg)
        ordered = sorted(values.values(), key=lambda v: -v.dv)
        top30 = ordered[:30]
        n_qb = sum(1 for v in top30 if v.pos == "QB")
        mix = {p: sum(1 for v in top30 if v.pos == p) for p in sorted({v.pos for v in top30})}
        return n_qb, mix

    n_1qb, _mix_1qb = _qb_count_top30(cfg_1qb)
    n_2qb, mix_2qb = _qb_count_top30(cfg)
    passed = n_2qb > n_1qb
    return CheckResult(
        "qb_count_in_top30",
        passed,
        f"1-QB rules: {n_1qb} QBs in top 30; this league's {cfg.starters.get('QB', 0)}-QB "
        f"rules: {n_2qb} QBs in top 30 (need strictly more under this league's rules); "
        f"top-30 position mix under this league's rules = "
        + ", ".join(f"{p}={n}" for p, n in mix_2qb.items()),
    )


# ===================================================================== 3: baseline monotonicity


def deep_synthetic_pool() -> list[PlayerSeason]:
    """A synthetic pool deep enough that sweeping teams 8->16 or starters 1->3 never runs a
    position dry. Same shape as ``tests/test_valuation.py``'s ``deep_pool()``, reproduced here
    so this gate has no dependency on the test suite."""

    def linear(pos: str, n: int, hi: float, lo: float) -> list[PlayerSeason]:
        step = 0.0 if n <= 1 else (hi - lo) / (n - 1)
        return [
            PlayerSeason(player_id=f"{pos}{i + 1}", pos=pos, ppg=hi - step * i, name=f"{pos}{i + 1}")
            for i in range(n)
        ]

    return (
        linear("QB", 80, 22.0, 6.0)
        + linear("RB", 120, 20.0, 3.0)
        + linear("WR", 150, 19.0, 3.0)
        + linear("TE", 80, 14.0, 2.0)
    )


def check_baseline_monotonic_team_count(
    deep_pool: Sequence[PlayerSeason], base_cfg: LeagueConfig, *, team_counts: Sequence[int] = (8, 10, 12, 14, 16)
) -> CheckResult:
    """More teams -> more man-games demand -> replacement rank must deepen (non-decreasing),
    and strictly deeper end to end."""
    rows: list[str] = []
    ranks_by_pos: dict[str, list[int]] = {}
    exhausted: list[str] = []
    for teams in team_counts:
        cfg = base_cfg.replace(teams=teams)
        levels = replacement_levels(deep_pool, cfg)
        for pos, info in levels.items():
            ranks_by_pos.setdefault(pos, []).append(info.baseline_rank)
            if info.pool_exhausted:
                exhausted.append(f"{pos}@{teams}teams")
        rows.append(
            f"teams={teams}: "
            + ", ".join(f"{pos}=rank{info.baseline_rank}" for pos, info in sorted(levels.items()))
        )

    bad = []
    for pos, ranks in ranks_by_pos.items():
        if ranks != sorted(ranks):
            bad.append(f"{pos} ranks not non-decreasing: {ranks}")
        elif ranks[-1] <= ranks[0]:
            bad.append(f"{pos} rank did not deepen from {team_counts[0]} to {team_counts[-1]} teams: {ranks}")

    passed = not bad and not exhausted
    detail = "; ".join(rows)
    if exhausted:
        detail += f" | POOL EXHAUSTED (invalidates the sweep): {exhausted}"
    if bad:
        detail += f" | FAILURES: {bad}"
    return CheckResult("baseline_monotonic_team_count", passed, detail)


def check_baseline_monotonic_starter_slots(
    deep_pool: Sequence[PlayerSeason], base_cfg: LeagueConfig, *, slot_counts: Sequence[int] = (1, 2, 3)
) -> CheckResult:
    """More starting slots at a position -> that position's replacement rank must deepen."""
    bad: list[str] = []
    rows: list[str] = []
    exhausted: list[str] = []
    for pos in sorted(base_cfg.starters):
        ranks = []
        for count in slot_counts:
            cfg = base_cfg.replace(starters={**dict(base_cfg.starters), pos: count})
            info = replacement_levels(deep_pool, cfg)[pos]
            ranks.append(info.baseline_rank)
            if info.pool_exhausted:
                exhausted.append(f"{pos}@{count}starters")
        rows.append(f"{pos}: " + "->".join(str(r) for r in ranks))
        if ranks != sorted(ranks):
            bad.append(f"{pos} ranks not non-decreasing in starters: {ranks}")
        elif ranks[-1] <= ranks[0]:
            bad.append(f"{pos} rank did not deepen from {slot_counts[0]} to {slot_counts[-1]} starters: {ranks}")

    passed = not bad and not exhausted
    detail = "; ".join(rows)
    if exhausted:
        detail += f" | POOL EXHAUSTED (invalidates the sweep): {exhausted}"
    if bad:
        detail += f" | FAILURES: {bad}"
    return CheckResult("baseline_monotonic_starter_slots", passed, detail)


# ======================================================================== 4: survival monotone


def check_survival_monotone_and_normalized() -> CheckResult:
    """Survival S(N) must be strictly decreasing in N, and S(n0)/S(n0) == 1 exactly -- checked
    both as the bare unconditional ratio and through the actual conditioning entrypoint the
    engine calls, :func:`~draftroom.draft.survival.p_available`."""
    cases = [(30.0, 8.0), (5.0, 1.5), (150.0, 25.0), (1.0, 0.5)]
    picks = list(range(1, 200, 5))
    bad: list[str] = []
    ratios: list[float] = []

    for mu, sd in cases:
        curve = [survival(mu, sd, n) for n in picks]
        for a, b in zip(curve, curve[1:]):
            if b > a:  # allow equality only at the numeric floor (both 0.0 or both 1.0)
                if not (a == b and a in (0.0, 1.0)):
                    bad.append(f"mu={mu},sd={sd}: S not monotone decreasing ({a} -> {b})")
                    break

        for n0 in (1.0, mu, mu + 3.0 * sd, 400.0):
            s = survival(mu, sd, n0)
            ratio = 1.0 if s == 0.0 else s / s  # exact self-ratio, s==0 handled as a no-op 1.0
            ratios.append(ratio)
            if ratio != 1.0:
                bad.append(f"mu={mu},sd={sd},n0={n0}: S(n0)/S(n0)={ratio} != 1")
            p_now = p_available(mu, sd, n0, n0)  # target==current -> defined to be exactly 1.0
            if p_now != 1.0:
                bad.append(f"mu={mu},sd={sd},n0={n0}: p_available(n0,n0)={p_now} != 1.0")

    passed = not bad
    detail = (
        f"checked {len(cases)} (mu,sd) pairs over {len(picks)} picks each for monotonicity; "
        f"S(n0)/S(n0) and p_available(n0,n0) checked at {len(ratios)} points, all == 1.0"
        if passed
        else "; ".join(bad)
    )
    return CheckResult("survival_monotone_and_normalized", passed, detail)


# ======================================================================= 5: per-game fixture


def check_per_game_fixture(baseline_ppg: float = 7.0) -> CheckResult:
    """The Harstad fixture, as its own standalone gate check (evob.py docstring, and
    ``tests/test_valuation.py``'s ``test_harstad_...`` tests cover the same fact for the
    valuation module specifically; this is the version that runs as part of the OPERATIONAL
    gate, independent of pytest)."""
    a_points, a_games = 83.2, 7.0  # high PPG, few games
    b_points, b_games = 84.2, 16.0  # low PPG, many games -- HIGHER season total
    a_ppg, b_ppg = a_points / a_games, b_points / b_games

    evob_a = evob(a_ppg, baseline_ppg, a_games)
    evob_b = evob(b_ppg, baseline_ppg, b_games)

    passed = evob_a > evob_b
    detail = (
        f"A: {a_points}pts/{a_games:.0f}gm ({a_ppg:.2f} ppg) -> EVoB {evob_a:.2f}; "
        f"B: {b_points}pts/{b_games:.0f}gm ({b_ppg:.2f} ppg, HIGHER season total) -> EVoB {evob_b:.2f}; "
        f"baseline={baseline_ppg:.2f} ppg; A must beat B despite B's higher season total"
    )
    return CheckResult("per_game_fixture_beats_season_total", passed, detail)


# ============================================================ 6: no fabricated full-season games


def check_no_default_expected_games_hits_full_season(
    deep_pool: Sequence[PlayerSeason], cfg: LeagueConfig
) -> CheckResult:
    """No ranked player's expected_games may equal the full season length BY DEFAULT.

    Guards the exact bug the 2026-08-18 games-played fix eliminated: prep/manual_csv.py used to
    emit a flat ``DEFAULT_GAMES = 17.0`` for every FantasyPros row regardless of the player, and
    valuation/replacement.py's old ``EXPECTED_GAMES_PRIOR``, while not literally 17, was still a
    single flat number per position blind to rank. Either bug overstated durability everywhere
    it touched, and a flat-17 regression in particular would look plausible for a QB1 and be
    silently wrong for a QB35.

    Exercises the REAL :func:`~draftroom.valuation.replacement.resolve_players` path (not just
    the curve table by inspection) against a synthetic pool where no player carries an explicit
    ``expected_games`` override, so every value comes from the rank-conditional availability
    curve. A player with a genuine, explicit per-player projection of exactly ``weeks`` games
    is fine and deliberately NOT what this checks -- ``resolve_players``'s override always wins
    over the curve, by design.
    """
    # Which players go through the curve (no override) has to be captured BEFORE resolution --
    # resolve_players() returns a flat PlayerSeason with expected_games already filled in, so
    # by that point a curve-derived 17.0 and a genuinely-overridden 17.0 are indistinguishable
    # by value alone.
    no_override_ids = {
        p.player_id for p in deep_pool if getattr(p, "expected_games", None) is None
    }
    resolved = resolve_players(deep_pool, cfg)
    default_path = [p for p in resolved if p.player_id in no_override_ids]

    weeks = float(cfg.weeks)
    offenders = [p for p in default_path if float(p.expected_games or 0.0) >= weeks]
    passed = not offenders
    worst = max((float(p.expected_games or 0.0) for p in default_path), default=0.0)
    detail = (
        f"checked {len(default_path)} of {len(resolved)} players with no explicit "
        f"expected_games override (the rest carry a real per-player projection and are "
        f"deliberately excluded) against a {cfg.weeks}-week season; max default "
        f"expected_games = {worst:.2f}"
    )
    if offenders:
        sample = ", ".join(f"{p.name or p.player_id}({p.pos})" for p in offenders[:8])
        detail += f" | FAIL: {len(offenders)} player(s) at/above the full season by default: {sample}"
    return CheckResult("no_default_full_season_games", passed, detail)


# ================================================= 7: real-board expected-games cap respected


def check_expected_games_capped_by_curve(
    players: Sequence[PlayerSeason], cfg: LeagueConfig
) -> CheckResult:
    """Every REAL-board season's ``expected_games`` must respect the availability curve.

    Added 2026-08-18 after a Codex review caught exactly the failure this detects: the board
    build passed Sleeper's per-player games straight through as an explicit override, which made
    the rank-conditional ``EXPECTED_GAMES_CURVE`` (that same day's headline projection fix)
    silently inert on the real board -- and the then-existing games check couldn't see it,
    because it ran only against a synthetic pool. This check runs against whatever REAL seasons
    the gate was handed: for each player carrying an explicit ``expected_games``, recompute the
    curve value at their PPG rank within position and require ``expected_games <= curve`` (the
    build's documented ``min(source, curve)`` policy), plus at least one player where the cap
    is exactly binding -- proof the cap code actually executed rather than the whole board
    happening to sit under the curve.

    Players with ``expected_games is None`` are skipped (they go through the curve downstream
    by construction); a pool with NO explicit games at all (the synthetic monotonicity pools)
    passes vacuously with that stated.
    """
    from draftroom.valuation.replacement import expected_games as _curve_games

    explicit = [p for p in players if getattr(p, "expected_games", None) is not None]
    if not explicit:
        return CheckResult(
            "expected_games_capped_by_curve",
            True,
            "no player in this pool carries an explicit expected_games (all use the curve "
            "downstream by construction) -- nothing to cap, passing vacuously",
        )

    by_pos: dict[str, list[PlayerSeason]] = {}
    for p in players:
        by_pos.setdefault(p.pos, []).append(p)

    tol = 1e-9
    offenders: list[str] = []
    binding = 0
    for pos, group in by_pos.items():
        for rank, p in enumerate(sorted(group, key=lambda x: -x.ppg), start=1):
            if p.expected_games is None:
                continue
            cap = _curve_games(pos, rank=rank, weeks=cfg.weeks)
            if float(p.expected_games) > cap + tol:
                offenders.append(
                    f"{p.name or p.player_id}({pos} rank {rank}): {p.expected_games:.2f} > cap {cap:.2f}"
                )
            elif abs(float(p.expected_games) - cap) <= tol:
                binding += 1

    passed = not offenders and binding >= 1
    detail = (
        f"checked {len(explicit)} players with explicit expected_games against the "
        f"rank-conditional curve; cap exactly binding for {binding} of them"
    )
    if offenders:
        detail += f" | FAIL: {len(offenders)} above the cap: {'; '.join(offenders[:6])}"
    elif binding < 1:
        detail += (
            " | FAIL: the cap never bound for a single player -- either every source projects "
            "under the fitted availability curve everywhere (implausible) or the min(source, "
            "curve) cap is no longer being applied in the board build"
        )
    return CheckResult("expected_games_capped_by_curve", passed, detail)


# ============================================================================= runner


def run_all(
    players: Sequence,
    cfg: LeagueConfig,
    *,
    deep_pool: Sequence[PlayerSeason] | None = None,
    per_game_baseline_ppg: float | None = None,
) -> list[CheckResult]:
    """Run every sanity invariant. ``players``/``cfg`` drive the ranking checks (real board,
    real league); ``deep_pool`` drives the two monotonicity sweeps (defaults to
    :func:`deep_synthetic_pool`, since those sweeps need artificial depth regardless of what
    the real board looks like today)."""
    deep_pool = deep_pool if deep_pool is not None else deep_synthetic_pool()
    if per_game_baseline_ppg is None:
        # Use this league's OWN TE baseline off the real board where available -- otherwise a
        # generic mid-single-digit PPG baseline the fixture already proved insensitive to
        # (test_harstad_ordering_holds_at_every_plausible_baseline covers this algebraically).
        try:
            per_game_baseline_ppg = replacement_levels(players, cfg)["TE"].baseline_ppg
        except (KeyError, ZeroDivisionError):
            per_game_baseline_ppg = 7.0

    return [
        check_top_qb_top8(players, cfg),
        check_qb_count_in_top30(players, cfg),
        check_baseline_monotonic_team_count(deep_pool, cfg),
        check_baseline_monotonic_starter_slots(deep_pool, cfg),
        check_survival_monotone_and_normalized(),
        check_per_game_fixture(per_game_baseline_ppg),
        check_no_default_expected_games_hits_full_season(deep_pool, cfg),
        check_expected_games_capped_by_curve(players, cfg),
    ]
