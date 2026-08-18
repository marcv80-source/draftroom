"""Mock-draft validation: does the real recommendation engine out-draft ADP-following bots?

Simulates full 15-round, 10-team snake drafts (the real, CONFIRMED league shape --
`data/league_manual.yaml`) with the real board built by `draftroom.validate.board` (real FFC
ADP joined onto real Sleeper season projections, valued through the real
replacement/EVoB pipeline). One seat is filled either by:

  * the real recommendation engine (`draftroom.draft.recommend.recommend`), taking its own
    top-ranked candidate every turn, or
  * an "ADP-following bot" -- the project's OWN opponent model
    (`draftroom.draft.opponents.opponent_pick_probabilities`, `LeagueCalibration.national_only()`,
    with live `PositionalRun` herd detection), the same model `recommend()` itself uses to
    forecast what the other nine managers will do. This is a more realistic "ADP bot" than a
    naive greedy-ADP picker: it already blends ADP, roster need, and herding exactly the way a
    real drafter does, and it is the model this repo already ships for "how does an opponent
    draft" -- reusing it here means the bot seats behave identically to how the engine's own
    Monte Carlo already assumes they behave.

The other nine seats are ALWAYS the bot model (an engine-vs-engine draft would prove nothing
about whether the engine beats a normal room).

OBJECTIVE FUNCTION: sum of real `dv` (season EVoB, points above the league's own real
replacement baseline) across the OPTIMAL starting lineup the final 15-man roster supports --
QB x2, RB x2, WR x3, TE x1, one RB/WR/TE flex (`cfg.starters`/`cfg.flex_*`) -- not the whole
bench. Bench depth that never starts scores zero points in real life, so crediting it would
reward hoarding over the players who actually win weeks. Because starters at each dedicated
position are disjoint and only the single flex slot creates any cross-position choice, filling
each dedicated slot with the position's own top-N-by-dv players and then the flex slot with the
single best leftover flex-eligible player is the OPTIMAL assignment, not a heuristic.

UNFILLED MANDATORY SLOTS ARE CHARGED, NOT FREE (fixed 2026-08-18). The original objective
implicitly scored an unfilled slot at 0 dv -- i.e. handed a roster that drafted ZERO quarterbacks
two replacement-level QBs for free, which is how a no-QB strategy once "won" a tournament at
639.3 while rostering zero QBs in 2000/2000 drafts. Two levers were considered: (a) enforcing
weekly lineup constraints in the sum -- already the case here, bench never scores and a 5th RB
cannot occupy a QB slot; and (b) pricing a truly unfilled slot at what the post-draft waiver
wire actually offers, minus a realism penalty. (b) is implemented by `waiver_fill_values`: an
unfilled slot is credited `min(0, median dv of the top-`cfg.teams` UNDRAFTED players at that
position)`. The median-of-the-contested-band (not the single best leftover) IS the realism
penalty -- ten teams work the same wire, so you do not get first pick of it -- expressed
structurally instead of as a magic constant. The cap at 0 denies hindsight credit for an
undrafted breakout (in historical mode a real breakout can carry hugely positive real dv;
"I'd have picked him up week 1" is exactly the look-ahead this tournament exists to exclude).

PASS BAR (spec): the engine's roster lands at or above the 65th percentile of rosters
achievable from that same draft slot. "Achievable from that slot" is estimated by the
bot-only baseline distribution for that slot -- what a fully ADP/need/herd-following manager
gets, facing nine others just like it, from each of the ten seats. The 2026 draft slot is
unknown until draft night, so every slot 1-10 is run and reported separately; a model that only
clears the bar from slot 1 is not ready (spec).

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\mock_draft_sim.py [--engine-reps N]
        [--baseline-reps N] [--n-sims N] [--seed N]
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import numpy as np  # noqa: E402

from draftroom.draft import opponents as opp  # noqa: E402
from draftroom.draft import snake  # noqa: E402
from draftroom.draft.recommend import BoardPlayer, recommend  # noqa: E402
from draftroom.draft.state import DraftState, Pick  # noqa: E402
from draftroom.draft.survival import PositionalRun  # noqa: E402
from draftroom.validate.board import build_real_board  # noqa: E402

PASS_PERCENTILE = 65.0


# ============================================================================== objective


def waiver_fill_values(undrafted_players, cfg) -> dict[str, float]:
    """dv credited to a mandatory lineup slot the drafted roster cannot fill, per position.

    `min(0.0, median dv of the top-cfg.teams undrafted players at the position)`. See the
    module docstring ("UNFILLED MANDATORY SLOTS ARE CHARGED") for why the median of the
    contested band is the waiver proxy and why the cap at 0 exists. A position with no
    undrafted players at all (should not happen -- every board here carries more players than
    the league drafts) falls back to the worst undrafted dv anywhere, still capped at 0.
    """
    undrafted = list(undrafted_players)
    all_dvs = [p.dv for p in undrafted]
    floor_dv = min(0.0, min(all_dvs)) if all_dvs else 0.0
    fill: dict[str, float] = {}
    for pos in set(cfg.starters) | set(cfg.flex_eligible):
        band = sorted((p.dv for p in undrafted if p.pos == pos), reverse=True)[: cfg.teams]
        if band:
            mid = band[(len(band) - 1) // 2]  # upper median: kind to the roster, still contested
            fill[pos] = min(0.0, mid)
        else:
            fill[pos] = floor_dv
    return fill


def starting_lineup_value(player_ids, players_by_id, cfg, *, waiver_fill=None) -> float:
    """Sum of `dv` across the OPTIMAL starting lineup this roster supports. See module
    docstring for why greedy-per-position-then-flex is exactly optimal here, not a heuristic.

    `waiver_fill` (from :func:`waiver_fill_values`) prices any mandatory slot the roster
    cannot fill from its own players. `None` preserves the legacy unfilled-slot-scores-0
    behavior for callers that guarantee full rosters; every scoring path in this tool and in
    `strategy_tournament.py` passes it.
    """
    by_pos: dict[str, list[BoardPlayer]] = {}
    for pid in player_ids:
        p = players_by_id[pid]
        by_pos.setdefault(p.pos, []).append(p)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda p: -p.dv)

    total = 0.0
    used: set[str] = set()
    for pos, need in cfg.starters.items():
        own = by_pos.get(pos, [])[:need]
        for p in own:
            total += p.dv
            used.add(p.player_id)
        if waiver_fill is not None and len(own) < need:
            total += (need - len(own)) * waiver_fill.get(pos, 0.0)

    flex_pool = sorted(
        (p for pos in cfg.flex_eligible for p in by_pos.get(pos, []) if p.player_id not in used),
        key=lambda p: -p.dv,
    )
    n_flex_own = min(cfg.flex_slots, len(flex_pool))
    for p in flex_pool[:n_flex_own]:
        total += p.dv
    if waiver_fill is not None and n_flex_own < cfg.flex_slots:
        best_flex_fill = max(
            (waiver_fill.get(pos, 0.0) for pos in cfg.flex_eligible), default=0.0
        )
        total += (cfg.flex_slots - n_flex_own) * best_flex_fill
    return total


def waiver_fill_for_draft(rosters: dict[int, list[str]], players_by_id, cfg) -> dict[str, float]:
    """The per-position waiver proxy for one FINISHED draft -- computed from what that draft
    actually left undrafted (it differs draft to draft), once, then reused for all 10 rosters."""
    drafted: set[str] = set()
    for ids in rosters.values():
        drafted.update(ids)
    undrafted = [p for pid, p in players_by_id.items() if pid not in drafted]
    return waiver_fill_values(undrafted, cfg)


# ============================================================================== room-2025 bots
#
# A second opponent room ("--room room2025"): bots whose POSITIONAL behavior is sampled from
# the real 2025 draft of this exact league (`data/draft_2025.csv`) -- 6 first-round QBs, the
# rounds-5-6 QB run, every team hoarding 3+ QBs (31 total, max 4 on one roster). The ADP-model
# room answers "does the engine beat a market-priced room"; this one answers "does it beat the
# room Marc actually sits in". Position first (empirical per-round frequencies), then the
# player at that position by the same ADP softmax the standard bot uses -- so the two rooms
# differ ONLY in positional pacing, never in within-position player choice.

ROOM_CSV = REPO_ROOT / "data" / "draft_2025.csv"

#: Hard per-team QB cap for room bots: the 2025 room's observed maximum (one team took 4).
ROOM_QB_CAP = 4


def load_room_profile(path: Path = ROOM_CSV) -> dict[int, dict[str, float]]:
    """{round: {pos: probability}} from the real 150 picks. Comment lines start with '#'."""
    counts: dict[int, dict[str, int]] = {}
    with open(path, encoding="utf-8") as fh:
        rows = csv.DictReader(line for line in fh if not line.startswith("#"))
        for r in rows:
            rnd = int(r["round"])
            pos = r["pos"].upper()
            counts.setdefault(rnd, {})[pos] = counts.setdefault(rnd, {}).get(pos, 0) + 1
    profile: dict[int, dict[str, float]] = {}
    for rnd, by_pos in counts.items():
        total = sum(by_pos.values())
        profile[rnd] = {pos: n / total for pos, n in by_pos.items()}
    return profile


def _room_bot_pick(rng, available, resolved, team_slot, pick_no, have, cfg, run, profile):
    """Sample a position from the room's real per-round frequencies, then a player at that
    position via the standard ADP softmax. Falls back to the ADP bot when every profiled
    position is illegal (hard constraint) or empty on the board."""
    rnd = snake.round_of(cfg.teams, pick_no)
    dist = profile.get(rnd) or profile[max(profile)]

    allowed = opp.hard_constraint_positions(have, cfg)
    on_board = {resolved[p.player_id][2] for p in available}
    legal = {}
    for pos, w in dist.items():
        if pos not in on_board:
            continue
        if allowed is not None and pos not in allowed:
            continue
        if pos == "QB" and have.get("QB", 0) >= ROOM_QB_CAP:
            continue
        legal[pos] = w
    if not legal:
        return _bot_pick(rng, available, resolved, team_slot, pick_no, have, cfg, run)

    positions = list(legal)
    weights = np.array([legal[p] for p in positions], dtype=float)
    weights = weights / weights.sum()
    pos_pick = positions[int(rng.choice(len(positions), p=weights))]

    pos_pool = [p for p in available if resolved[p.player_id][2] == pos_pick]
    # Within the position: the same softmax the ADP bot runs, with need/constraint state
    # blanked (the position decision has already been made by the empirical profile).
    return _bot_pick(rng, pos_pool, resolved, team_slot, pick_no, {}, cfg, run)


# ============================================================================== draft loop


def _bot_pick(rng, available, resolved, team_slot, pick_no, have, cfg, run):
    probs = opp.opponent_pick_probabilities(
        available, team_slot=team_slot, pick_no=pick_no, have=have, cfg=cfg, run=run, resolved=resolved
    )
    if not probs:  # defensive -- should not happen, opponents.py already falls back internally
        probs = {p.player_id: 1.0 for p in available}
    pids = list(probs.keys())
    p_arr = np.array([probs[pid] for pid in pids], dtype=float)
    p_arr = p_arr / p_arr.sum()
    return pids[int(rng.choice(len(pids), p=p_arr))]


def run_one_draft(
    *,
    seed: int,
    engine_slot: int | None,
    players_by_id: dict[str, BoardPlayer],
    full_players: list[BoardPlayer],
    resolved: dict[str, tuple[float, float | None, str]],
    cfg,
    n_sims: int,
    room_profile: dict[int, dict[str, float]] | None = None,
) -> dict[int, list[str]]:
    """One full 15-round, `cfg.teams`-team snake draft. `engine_slot=None` means every seat is
    the bot model (the baseline run); otherwise that one seat calls the real `recommend()`.
    `room_profile` switches every bot seat from the ADP model to the room-2025 bot.

    Returns {team_slot: [player_id, ...]} for every team's final roster.
    """
    rng = np.random.default_rng(seed)
    pool: dict[str, BoardPlayer] = dict(players_by_id)
    have: dict[int, dict[str, int]] = {t: {} for t in range(1, cfg.teams + 1)}
    rosters: dict[int, list[str]] = {t: [] for t in range(1, cfg.teams + 1)}
    run = PositionalRun()
    # `LeagueConfig` has no separate "rounds" field -- `data/league_manual.yaml`'s own
    # `rounds: 15` IS `cfg.roster_size` (every roster spot gets drafted exactly once: 9
    # starters + 1 flex + 6 bench = 15 = 15 rounds), so that is the round count everywhere here.
    rounds = cfg.roster_size
    state = DraftState(teams=cfg.teams, rounds=rounds, my_slot=engine_slot or 1)

    total_picks = cfg.teams * rounds
    for pick_no in range(1, total_picks + 1):
        slot = snake.slot_on_clock(cfg.teams, pick_no)
        available = list(pool.values())
        if not available:
            break

        if slot == engine_slot:
            state.current_pick = pick_no
            state.my_slot = slot
            rec = recommend(state, cfg, full_players, n_sims=n_sims, seed=seed * 100_000 + pick_no)
            chosen = rec.candidates[0].player_id if rec.candidates else max(available, key=lambda p: p.dv).player_id
        elif room_profile is not None:
            chosen = _room_bot_pick(
                rng, available, resolved, slot, pick_no, have[slot], cfg, run, room_profile
            )
        else:
            chosen = _bot_pick(rng, available, resolved, slot, pick_no, have[slot], cfg, run)

        pos = pool[chosen].pos
        pool.pop(chosen)
        have[slot][pos] = have[slot].get(pos, 0) + 1
        rosters[slot].append(chosen)
        state.picks[pick_no] = Pick(pick_no=pick_no, team_slot=slot, player_id=chosen)
        run.observe(pos, remaining=list(pool.values()))

    return rosters


# ==================================================================================== main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine-reps", type=int, default=15, help="engine-seat drafts PER SLOT")
    ap.add_argument(
        "--baseline-reps", type=int, default=1000,
        help="bot-only (all 10 seats) drafts -- each rep yields one baseline sample for EVERY slot",
    )
    ap.add_argument("--n-sims", type=int, default=25, help="recommend()'s internal Monte Carlo trial count")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument(
        "--room", choices=("adp", "room2025"), default="adp",
        help="opponent room: 'adp' = the standard ADP/need/herd bot model; 'room2025' = bots "
             "whose positional pacing is sampled from this league's real 2025 draft "
             "(6 first-round QBs, the rounds-5-6 QB run, 3+ QBs per team)",
    )
    args = ap.parse_args()

    room_profile = load_room_profile() if args.room == "room2025" else None

    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)

    print("=" * 100)
    print(f"MOCK DRAFT: engine vs. {'room-2025' if room_profile else 'ADP-following'} bots, every draft slot 1-10")
    print("=" * 100)

    real = build_real_board()
    cfg = real.cfg
    players_by_id = {p.player_id: p for p in real.players}
    full_players = list(real.players)
    resolved = {p.player_id: (p.adp, p.stdev, p.pos) for p in real.players}

    print(
        f"\nboard: {len(full_players)} real players (real season projections x real FFC ADP), "
        f"league: {cfg.teams} teams, starters={dict(cfg.starters)}, flex={cfg.flex_slots}x"
        f"{sorted(cfg.flex_eligible)}, rounds={cfg.roster_size} (roster_size)"
    )
    print(f"objective: sum of real dv across the OPTIMAL starting lineup (see module docstring)")
    print(
        f"bots: draftroom.draft.opponents.opponent_pick_probabilities "
        f"(LeagueCalibration.national_only(), live PositionalRun herd detection)"
    )
    print(f"engine: draftroom.draft.recommend.recommend(n_sims={args.n_sims}), top candidate taken each turn")

    # ------------------------------------------------------------ baseline: bot-only drafts
    t0 = time.perf_counter()
    baseline_by_slot: dict[int, list[float]] = {s: [] for s in range(1, cfg.teams + 1)}
    for i in range(args.baseline_reps):
        rosters = run_one_draft(
            seed=args.seed + i,
            engine_slot=None,
            players_by_id=players_by_id,
            full_players=full_players,
            resolved=resolved,
            cfg=cfg,
            n_sims=args.n_sims,
            room_profile=room_profile,
        )
        fill = waiver_fill_for_draft(rosters, players_by_id, cfg)
        for slot, ids in rosters.items():
            baseline_by_slot[slot].append(
                starting_lineup_value(ids, players_by_id, cfg, waiver_fill=fill)
            )
        if (i + 1) % max(1, args.baseline_reps // 10) == 0:
            print(f"  baseline draft {i + 1}/{args.baseline_reps} done ({time.perf_counter() - t0:.1f}s elapsed)")
    baseline_elapsed = time.perf_counter() - t0
    print(f"baseline: {args.baseline_reps} bot-only drafts in {baseline_elapsed:.1f}s "
          f"({baseline_elapsed / max(1, args.baseline_reps):.4f}s/draft) -> "
          f"{args.baseline_reps} samples per slot")

    # ------------------------------------------------------------------- engine: per-slot reps
    engine_by_slot: dict[int, list[float]] = {s: [] for s in range(1, cfg.teams + 1)}
    t1 = time.perf_counter()
    for slot in range(1, cfg.teams + 1):
        slot_t0 = time.perf_counter()
        for i in range(args.engine_reps):
            rosters = run_one_draft(
                seed=args.seed + 10_000 * slot + i,
                engine_slot=slot,
                players_by_id=players_by_id,
                full_players=full_players,
                resolved=resolved,
                cfg=cfg,
                n_sims=args.n_sims,
                room_profile=room_profile,
            )
            fill = waiver_fill_for_draft(rosters, players_by_id, cfg)
            engine_by_slot[slot].append(
                starting_lineup_value(rosters[slot], players_by_id, cfg, waiver_fill=fill)
            )
        print(
            f"  slot {slot:2d}: {args.engine_reps} engine drafts in {time.perf_counter() - slot_t0:.1f}s"
        )
    engine_elapsed = time.perf_counter() - t1
    print(f"engine: {cfg.teams} slots x {args.engine_reps} reps = "
          f"{cfg.teams * args.engine_reps} drafts in {engine_elapsed:.1f}s")

    # =================================================================================== report
    print("\n" + "=" * 100)
    print(f"PER-SLOT RESULT  (pass bar: engine median >= {PASS_PERCENTILE:.0f}th pct of the bot-only baseline)")
    print("=" * 100)
    header = (
        f"{'slot':>4}  {'baseline (bot-only), n='+str(args.baseline_reps):>28}  "
        f"{'engine, n='+str(args.engine_reps):>26}  {'engine pct in baseline':>22}  {'verdict':>8}"
    )
    print(header)
    print("-" * len(header))

    n_pass = 0
    for slot in range(1, cfg.teams + 1):
        base = np.array(baseline_by_slot[slot])
        eng = np.array(engine_by_slot[slot])
        base_mean, base_std = base.mean(), base.std(ddof=1)
        eng_mean, eng_std = eng.mean(), eng.std(ddof=1)
        eng_median = float(np.median(eng))

        pct = 100.0 * float((base <= eng_median).mean())
        # Per-rep percentile too, to show the engine's OWN spread against the same baseline
        # (the "noise level" the report has to state) rather than just one aggregate number.
        per_rep_pct = [100.0 * float((base <= v).mean()) for v in eng]

        verdict = "PASS" if pct >= PASS_PERCENTILE else "FAIL"
        n_pass += verdict == "PASS"
        print(
            f"{slot:>4}  {base_mean:9.1f} +/- {base_std:6.1f}          "
            f"{eng_mean:9.1f} +/- {eng_std:6.1f}         "
            f"{pct:8.1f}th (median)      {verdict:>8}"
        )
        print(
            f"      per-rep engine percentile range: [{min(per_rep_pct):.1f}, {max(per_rep_pct):.1f}]"
            f"th, mean {sum(per_rep_pct) / len(per_rep_pct):.1f}th  "
            f"(engine noise: std/sqrt(n)={eng_std / (len(eng) ** 0.5):.2f} pts on a base mean "
            f"of {base_mean:.1f})"
        )

    print("-" * len(header))
    print(f"\n{n_pass}/{cfg.teams} slots clear the {PASS_PERCENTILE:.0f}th-percentile bar.")
    return 0 if n_pass == cfg.teams else 1


if __name__ == "__main__":
    sys.exit(main())
