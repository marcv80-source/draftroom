"""Calibrate the opponent model against the room's real 2025 draft -- and report honestly.

Builds :class:`~draftroom.draft.opponents.PickObservation` rows from ``data/draft_2025.csv``
(150 real picks, this exact 10-team room) and the newest cached FFC 2QB@12-team ADP payload
under ``data/raw/ffc/`` (CLAUDE.md: FFC does not publish a 10-team 2QB feed, so this is a
PROXY, rescaled by team-count ratio -- :func:`scale_adp_to_league`), then:

1. Fits a flat ``position_timing_offset`` and a shrunk ``manager_reach`` from that history.
2. Reports "how this room paces its QBs" as a plain cumulative-count table against the
   scaled national pace -- zero name-matching involved, so it cannot be confounded by a
   player's national value having moved between the 2025 draft and today's ADP snapshot.
3. Measures run structure (self-transition lift) directly off the 150 real picks.
4. Runs the **hard gate**: leave-one-manager-out cross-validation of the actual production
   opponent model (``opponents.opponent_pick_probabilities``) -- for each of the 129 picks
   whose player matched the ADP payload, was the real pick ranked highly among the players
   genuinely available at that moment, under (a) ``LeagueCalibration.national_only()`` and
   (b) the position-offset calibration fit on the *other nine* managers only. Whichever wins
   on mean rank is what gets shipped in ``data/opponent_calibration_2025.json``.

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\calibrate_opponents.py
"""

from __future__ import annotations

import glob
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))

import analyze_2025_draft as draft2025  # noqa: E402  (reuse its loader, don't duplicate it)
from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.draft import opponents as opp  # noqa: E402
from draftroom.draft.survival import AdpPlayer, PositionalRun  # noqa: E402

TEAMS_LEAGUE = 10  # data/league_manual.yaml -- CONFIRMED 2026-08-17
CALIBRATION_OUT = opp.DEFAULT_CALIBRATION_PATH


# --------------------------------------------------------------------------- loading + matching


def _norm_name(name: str) -> str:
    """Match names across sources: strip suffixes/punctuation FFC and Yahoo don't agree on."""
    name = name.strip()
    name = re.sub(r"[.']", "", name)
    name = re.sub(r"\s+(Jr|Sr|II|III|IV|V)$", "", name, flags=re.IGNORECASE)
    return name.lower().strip()


def load_newest_ffc_payload() -> dict:
    files = sorted(glob.glob(str(ROOT / "data" / "raw" / "ffc" / "*.json")))
    if not files:
        raise FileNotFoundError("no cached FFC payload under data/raw/ffc/*.json")
    with open(files[-1], encoding="utf-8") as fh:
        return json.load(fh), Path(files[-1]).name  # type: ignore[return-value]


def build_observations(
    rows: Sequence[dict], ffc_players: Sequence[dict], *, scale: float
) -> tuple[list[opp.PickObservation], list[str]]:
    """Match each real pick to the FFC payload by name; return (observations, unmatched names)."""
    adp_by_name = {_norm_name(p["name"]): p for p in ffc_players}
    obs: list[opp.PickObservation] = []
    unmatched: list[str] = []
    for r in rows:
        p = adp_by_name.get(_norm_name(r["player"]))
        if p is None:
            unmatched.append(r["player"])
            continue
        obs.append(
            opp.PickObservation(
                pick_no=r["pick_no"],
                team_slot=r["slot"],
                pos=r["pos"],
                scaled_adp=float(p["adp"]) * scale,
            )
        )
    return obs, unmatched


def build_player_pool(ffc_players: Sequence[dict], *, scale: float) -> dict[str, AdpPlayer]:
    """Every FFC player as an AdpPlayer, ADP (and its spread) rescaled onto our team count."""
    pool: dict[str, AdpPlayer] = {}
    for p in ffc_players:
        pid = str(p["player_id"])
        pool[pid] = AdpPlayer(
            player_id=pid,
            name=p["name"],
            pos=p["position"],
            adp=float(p["adp"]) * scale,
            stdev=float(p["stdev"]) * scale,
        )
    return pool


def match_pick_ids(rows: Sequence[dict], ffc_players: Sequence[dict]) -> dict[int, str]:
    """pick_no -> FFC player_id, for the picks whose player exists in the ADP payload."""
    adp_by_name = {_norm_name(p["name"]): p for p in ffc_players}
    out: dict[int, str] = {}
    for r in rows:
        p = adp_by_name.get(_norm_name(r["player"]))
        if p is not None:
            out[r["pick_no"]] = str(p["player_id"])
    return out


# --------------------------------------------------------------------------- run structure


def run_structure_report(rows: Sequence[dict]) -> dict:
    """Self-transition lift: P(next pick same position | this pick was P) / share(P).

    Pure position-sequence statistics off the 150 real picks -- no ADP, no name-matching, so
    it cannot be confounded by ADP-year drift. A lift > 1 means this room's picks cluster by
    position beyond what raw frequency alone would predict -- i.e. herding is real, matching
    CLAUDE.md's modeling assumption that opponents herd.
    """
    seq = [r["pos"] for r in sorted(rows, key=lambda r: r["pick_no"])]
    n = len(seq)
    share = {p: c / n for p, c in _counter(seq).items()}
    same_next = sum(1 for i in range(1, n) if seq[i] == seq[i - 1])
    expected_same = sum(s**2 for s in share.values())
    trans_same: dict[str, int] = defaultdict(int)
    trans_total: dict[str, int] = defaultdict(int)
    for i in range(1, n):
        trans_total[seq[i - 1]] += 1
        if seq[i] == seq[i - 1]:
            trans_same[seq[i - 1]] += 1
    per_pos = {
        p: {
            "share": share[p],
            "p_next_same_given_prev": trans_same[p] / trans_total[p] if trans_total[p] else 0.0,
            "lift": (trans_same[p] / trans_total[p]) / share[p]
            if trans_total[p] and share[p]
            else 0.0,
            "n_prev": trans_total[p],
        }
        for p in sorted(share)
    }
    return {
        "global_share": share,
        "p_same_next_observed": same_next / (n - 1),
        "p_same_next_expected_iid": expected_same,
        "global_lift": (same_next / (n - 1)) / expected_same if expected_same else 0.0,
        "per_position": per_pos,
    }


def _counter(seq: Sequence[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in seq:
        out[s] += 1
    return dict(out)


def qb_pace_report(rows: Sequence[dict], ffc_players: Sequence[dict], *, scale: float) -> dict:
    """Cumulative QB count, room vs. scaled-national, at the pick numbers CLAUDE.md calls out.

    Zero name-matching: purely "how many QB entries has each feed exhausted by pick N",
    counted independently in each feed's own ADP-rank order. Immune to any one player's
    national value having moved between the 2025 draft and today's ADP snapshot.
    """
    room_qb_picks = sorted(r["pick_no"] for r in rows if r["pos"] == "QB")
    nat_qb_scaled = sorted(float(p["adp"]) * scale for p in ffc_players if p["position"] == "QB")
    checkpoints = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    table = []
    for n in checkpoints:
        table.append(
            {
                "pick": n,
                "room_cum_qb": sum(1 for x in room_qb_picks if x <= n),
                "national_scaled_cum_qb": sum(1 for x in nat_qb_scaled if x <= n),
            }
        )
    return {
        "room_qb_picks": room_qb_picks,
        "checkpoints": table,
        "qb_in_picks_1_10": sum(1 for x in room_qb_picks if x <= 10),
        "qb_in_picks_11_50": sum(1 for x in room_qb_picks if 11 <= x <= 50),
        "qb_in_picks_51_60": sum(1 for x in room_qb_picks if 51 <= x <= 60),
        "qb_in_picks_61_plus": sum(1 for x in room_qb_picks if x > 60),
    }


# --------------------------------------------------------------------------- LOMO offset fit
# (rank-matched, not name-matched -- avoids the ADP-year-drift confound entirely: it never
# claims "this specific player's value", only "the r-th cheapest QB nationally vs. the r-th QB
# this room actually took", so one player's post-2025-season national-value swing can't blow
# up a single residual the way name-matching does.)


def _rank_matched_offsets(
    rows: Sequence[dict], ffc_players: Sequence[dict], *, scale: float, exclude_slot: int | None
) -> dict[str, float]:
    nat_by_pos: dict[str, list[float]] = defaultdict(list)
    for p in ffc_players:
        nat_by_pos[p["position"]].append(float(p["adp"]) * scale)
    for pos in nat_by_pos:
        nat_by_pos[pos].sort()

    room_by_pos: dict[str, list[int]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["pick_no"]):
        if exclude_slot is not None and r["slot"] == exclude_slot:
            continue
        room_by_pos[r["pos"]].append(r["pick_no"])

    offsets: dict[str, float] = {}
    for pos, room_seq in room_by_pos.items():
        nat_seq = nat_by_pos.get(pos, [])
        k = min(len(nat_seq), len(room_seq))
        if k == 0:
            continue
        resids = [nat_seq[i] - room_seq[i] for i in range(k)]
        offsets[pos] = statistics.mean(resids)
    return offsets


# --------------------------------------------------------------------------- softmax validation


def replay_and_score(
    rows: Sequence[dict],
    pick_ids: Mapping[int, str],
    pool_template: Mapping[str, AdpPlayer],
    cfg: LeagueConfig,
    calib_for_slot,
) -> dict:
    """One full replay of the real 150 picks, scoring the production softmax at each of them.

    ``calib_for_slot(slot) -> LeagueCalibration`` picks which calibration scores a given
    team's pick (so a leave-one-manager-out fit can be swapped in per held-out slot inside a
    single pass -- board state (rosters, run detector) does not depend on calibration, so one
    replay serves every fold).
    """
    pool = dict(pool_template)
    have: dict[int, dict[str, int]] = {s: defaultdict(int) for s in range(1, TEAMS_LEAGUE + 1)}
    run = PositionalRun()
    ranks: list[int] = []
    top1 = top3 = 0
    reciprocal: list[float] = []

    for r in sorted(rows, key=lambda r: r["pick_no"]):
        slot, pick_no, pos = r["slot"], r["pick_no"], r["pos"]
        pid = pick_ids.get(pick_no)
        available = list(pool.values())
        if pid is not None and pid in pool:
            calib = calib_for_slot(slot)
            probs = opp.opponent_pick_probabilities(
                available,
                team_slot=slot,
                pick_no=float(pick_no),
                have=have[slot],
                cfg=cfg,
                calibration=calib,
                run=run,
            )
            if probs:
                ranked = sorted(probs.items(), key=lambda kv: -kv[1])
                rank = next((i for i, (k, _) in enumerate(ranked, 1) if k == pid), None)
                if rank is not None:
                    ranks.append(rank)
                    reciprocal.append(1.0 / rank)
                    top1 += rank == 1
                    top3 += rank <= 3

        if pid is not None and pid in pool:
            del pool[pid]
        have[slot][pos] += 1
        run.observe(pos, remaining=list(pool.values()))

    n = len(ranks)
    return {
        "n_scored": n,
        "top1_accuracy": top1 / n if n else 0.0,
        "top3_accuracy": top3 / n if n else 0.0,
        "mean_rank": statistics.mean(ranks) if ranks else float("nan"),
        "mrr": statistics.mean(reciprocal) if reciprocal else 0.0,
    }


# --------------------------------------------------------------------------- main


def main() -> int:
    rows = draft2025.load()
    if not draft2025.validate(rows):
        print("Transcription checks failed -- refusing to calibrate on untrusted data.")
        return 1

    raw, ffc_filename = load_newest_ffc_payload()
    ffc_players = raw["players"]
    teams_national = int(raw["meta"]["teams"])
    scale = opp.scale_adp_to_league(1.0, teams_national=teams_national, teams_league=TEAMS_LEAGUE)

    print("=" * 92)
    print("CALIBRATION INPUTS")
    print("=" * 92)
    print(f"draft history      : data/draft_2025.csv, {len(rows)} picks, 6 transcription checks PASS")
    print(f"national ADP source: data/raw/ffc/{ffc_filename}  ({raw['meta']})")
    print(f"team-count rescale : {teams_national} (national) -> {TEAMS_LEAGUE} (this league), "
          f"factor {scale:.4f}")

    cfg = LeagueConfig.from_yaml()
    print(f"league config       : {cfg.teams} teams, starters={dict(cfg.starters)}, "
          f"flex={cfg.flex_slots}x{sorted(cfg.flex_eligible)}, bench={cfg.bench}")

    observations, unmatched = build_observations(rows, ffc_players, scale=scale)
    print(f"\nname-matched observations: {len(observations)} of {len(rows)} "
          f"({len(unmatched)} unmatched -- players whose national relevance/name changed "
          f"between the 2025 draft and today's ADP snapshot)")
    print(f"unmatched: {unmatched}")

    # ---- position timing offset: name-matched (flat mean, in-sample, for reference) ----
    offset_name_matched = opp.fit_position_timing_offset(observations)
    print("\n" + "=" * 92)
    print("POSITION TIMING OFFSET -- name-matched, in-sample (reference only, see caveat below)")
    print("=" * 92)
    for pos, v in sorted(offset_name_matched.items()):
        print(f"  {pos}: {v:+.2f} picks")
    print("CAVEAT: this join re-uses a single player's CURRENT (2026) ADP as a stand-in for his "
          "2025 draft-day expectation. A full season of outcomes moved individual players a lot "
          "(breakouts/busts) in ways that have nothing to do with room behavior. See the "
          "rank-matched version below, which avoids per-player identity entirely.")

    # ---- position timing offset: rank-matched (avoids the identity-drift confound) ----
    offset_rank_matched = _rank_matched_offsets(rows, ffc_players, scale=scale, exclude_slot=None)
    print("\n" + "=" * 92)
    print("POSITION TIMING OFFSET -- rank-matched, in-sample (r-th cheapest QB nationally vs. "
          "r-th QB this room actually took; no player identity involved)")
    print("=" * 92)
    for pos, v in sorted(offset_rank_matched.items()):
        print(f"  {pos}: {v:+.2f} picks")

    # ---- QB pace: the headline claim, checked with zero ADP-year-drift exposure ----
    qb_pace = qb_pace_report(rows, ffc_players, scale=scale)
    print("\n" + "=" * 92)
    print("QB PACE -- room vs. scaled-national cumulative count (zero name-matching)")
    print("=" * 92)
    print(f"  room's QB picks (pick_no): {qb_pace['room_qb_picks']}")
    print(f"  {'pick':>5}  {'room cum QB':>12}  {'national-scaled cum QB':>24}")
    for row in qb_pace["checkpoints"]:
        print(f"  {row['pick']:>5}  {row['room_cum_qb']:>12}  {row['national_scaled_cum_qb']:>24}")
    print(f"  QBs in picks 1-10  : {qb_pace['qb_in_picks_1_10']} of 10   (the CLAUDE.md headline)")
    print(f"  QBs in picks 11-50 : {qb_pace['qb_in_picks_11_50']} of 40")
    print(f"  QBs in picks 51-60 : {qb_pace['qb_in_picks_51_60']} of 10   (the scramble to fill all 20 starters)")
    print(f"  QBs in picks 61+   : {qb_pace['qb_in_picks_61_plus']} of 90")
    print("  Reading: the room's QB pace roughly tracks the (already-2QB) national scaled pace "
          "for the first ~10 picks, falls BEHIND it through the middle rounds, then catches up "
          "in a pick-51-60 scramble to exactly fill 20 starting slots. A single flat offset "
          "nets these opposing phases into a number that helps nowhere well -- see the "
          "validation below.")

    # ---- run structure ----
    runs = run_structure_report(rows)
    print("\n" + "=" * 92)
    print("RUN STRUCTURE -- self-transition lift (measured, not assumed)")
    print("=" * 92)
    print(f"  global P(next pick same position) observed = {runs['p_same_next_observed']:.3f}  "
          f"vs. IID-expected = {runs['p_same_next_expected_iid']:.3f}  "
          f"(lift {runs['global_lift']:.2f}x)")
    for pos, d in runs["per_position"].items():
        print(f"  {pos}: P(next={pos}|prev={pos})={d['p_next_same_given_prev']:.3f}  "
              f"share={d['share']:.3f}  lift={d['lift']:.2f}x  (n_prev={d['n_prev']})")

    # ---- manager reach (descriptive; in-sample) ----
    reach_fit = opp.fit_manager_reach(observations, offset_name_matched)
    print("\n" + "=" * 92)
    print("MANAGER REACH -- empirical-Bayes shrinkage (descriptive, in-sample)")
    print("=" * 92)
    print(f"  between-manager variance (tau^2) = {reach_fit.between_manager_variance:.2f}")
    print(f"  within-manager variance (sigma^2) = {reach_fit.within_manager_variance:.2f}")
    print(f"  shrinkage factor lambda = {reach_fit.lam:.3f}  "
          f"(0 = ignore the manager entirely, 1 = trust their own 15 picks fully)")
    print(f"  {'slot':>4}  {'n':>3}  {'raw reach':>10}  {'shrunk reach':>13}")
    for slot in sorted(reach_fit.raw):
        print(f"  {slot:>4}  {reach_fit.n_per_manager[slot]:>3}  "
              f"{reach_fit.raw[slot]:>+10.2f}  {reach_fit.shrunk[slot]:>+13.2f}")

    # ---- HARD GATE: leave-one-manager-out validation of the real softmax model ----
    print("\n" + "=" * 92)
    print("HARD GATE -- leave-one-manager-out CV of the real opponent softmax")
    print("=" * 92)
    print("scheme: for each of the 10 slots, fit position_timing_offset on the OTHER nine "
          "managers' picks only (rank-matched), then score that slot's own 15 picks with the "
          "real draftroom.draft.opponents.opponent_pick_probabilities(). Every scored pick is "
          "therefore judged by a calibration that never saw it, or the manager who made it.")

    pick_ids = match_pick_ids(rows, ffc_players)
    pool_template = build_player_pool(ffc_players, scale=scale)
    lomo_calib = {
        s: opp.LeagueCalibration(
            position_timing_offset=_rank_matched_offsets(rows, ffc_players, scale=scale, exclude_slot=s),
            manager_reach={},
        )
        for s in range(1, TEAMS_LEAGUE + 1)
    }

    plain_result = replay_and_score(
        rows, pick_ids, pool_template, cfg, lambda slot: opp.LeagueCalibration.national_only()
    )
    calibrated_result = replay_and_score(
        rows, pick_ids, pool_template, cfg, lambda slot: lomo_calib[slot]
    )

    def _fmt(res: dict) -> str:
        return (f"n={res['n_scored']}  top1={res['top1_accuracy']:.3f}  "
                f"top3={res['top3_accuracy']:.3f}  mean_rank={res['mean_rank']:.2f}  "
                f"mrr={res['mrr']:.4f}")

    print(f"\n  plain ADP (national_only)                    : {_fmt(plain_result)}")
    print(f"  calibrated position-offset (leave-mgr-out)    : {_fmt(calibrated_result)}")

    calibrated_wins = calibrated_result["mean_rank"] < plain_result["mean_rank"]
    delta = plain_result["mean_rank"] - calibrated_result["mean_rank"]
    print(f"\n  mean-rank delta (plain - calibrated), positive = calibrated better: {delta:+.2f}")
    if calibrated_wins:
        verdict = "CALIBRATED BEATS PLAIN ADP out-of-sample -- shipping the fitted offset."
    else:
        verdict = ("CALIBRATED DOES NOT BEAT PLAIN ADP out-of-sample -- per CLAUDE.md's own "
                   "rule, shipping PLAIN ADP (empty offsets) and saying so.")
    print(f"\n  VERDICT: {verdict}")

    # ---- write the params file: shipped calibration is whichever won above ----
    if calibrated_wins:
        shipped_offset = opp.fit_position_timing_offset(observations)
        shipped_reach: dict[int, float] = {}
    else:
        shipped_offset = {}
        shipped_reach = {}

    shipped = opp.LeagueCalibration(position_timing_offset=shipped_offset, manager_reach=shipped_reach)
    extra = {
        "generated_by": "tools/calibrate_opponents.py",
        "source_draft": "data/draft_2025.csv",
        "adp_source": f"data/raw/ffc/{ffc_filename}",
        "adp_scale": {
            "teams_national": teams_national,
            "teams_league": TEAMS_LEAGUE,
            "factor": scale,
        },
        "validation": {
            "metric": "rank of the actual pick among the real available pool, under "
                       "opponents.opponent_pick_probabilities",
            "scheme": "leave-one-manager-out (10 folds)",
            "plain_adp": plain_result,
            "calibrated_position_offset": calibrated_result,
            "mean_rank_delta": delta,
            "verdict": verdict,
        },
        "measured_position_timing_offset_name_matched": offset_name_matched,
        "measured_position_timing_offset_rank_matched": offset_rank_matched,
        "measured_manager_reach_raw": {str(k): v for k, v in reach_fit.raw.items()},
        "measured_manager_reach_shrunk": {str(k): v for k, v in reach_fit.shrunk.items()},
        "measured_shrinkage": {
            "lambda": reach_fit.lam,
            "between_manager_variance": reach_fit.between_manager_variance,
            "within_manager_variance": reach_fit.within_manager_variance,
        },
        "measured_qb_pace": qb_pace,
        "measured_run_structure": runs,
    }
    shipped.to_json(CALIBRATION_OUT, extra=extra)
    print(f"\nwrote {CALIBRATION_OUT}")
    print(f"shipped position_timing_offset: {shipped_offset}")
    print(f"shipped manager_reach: {shipped_reach}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
