"""THROWAWAY / diagnostic only -- answers one question for the coordinator:

    Does turning on the per-game yardage bonus (`score_statline_with_bonus`, landed by the
    bonus agent) fix the `qb_count_in_top30` sanity-invariant FAIL from the validation report?

Reads ONLY: `draftroom.prep.scoring.score_statline_with_bonus`, `draftroom.valuation.bonuses`
(`load_bonus_schedule`, `load_curves`), and the already-fitted `data/bonus_curves.json`. Does
NOT edit `prep/scoring.py`, `valuation/*`, `data/bonus_curves.json`, or any invariant threshold.
Does NOT call `prep/fetch_all.py` or touch the network -- reads only the already-cached raw
files under `data/raw/` (the same ones `draftroom.validate.board.build_real_board` reads), so it
cannot disturb another agent's cache.

This intentionally duplicates a small slice of `draftroom.validate.board.build_real_board`'s
join logic rather than editing that module, per the coordinator's "standalone throwaway script,
read-only" instruction -- the only difference from `build_real_board` is which scoring function
computes each player's season total (`score_statline` vs `score_statline_with_bonus`).

Run:
    C:\\dev\\draftroom\\.venv\\Scripts\\python.exe tools\\compare_bonus_effect.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.config import LeagueConfig  # noqa: E402
from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk  # noqa: E402
from draftroom.prep.ffc_client import parse_adp_rows  # noqa: E402
from draftroom.prep.http import load_latest_raw  # noqa: E402
from draftroom.prep.scoring import score_statline, score_statline_with_bonus  # noqa: E402
from draftroom.prep.sleeper_client import SKILL_POSITIONS, to_statlines  # noqa: E402
from draftroom.valuation.bonuses import load_bonus_schedule, load_curves  # noqa: E402
from draftroom.valuation.evob import compute_draft_values  # noqa: E402
from draftroom.valuation.replacement import PlayerSeason, replacement_levels  # noqa: E402


def build_seasons(cfg: LeagueConfig, *, with_bonus: bool):
    """Same join `draftroom.validate.board.build_real_board` does, scored either with plain
    `score_statline` or with `score_statline_with_bonus` -- the only difference."""
    sleeper_raw = load_latest_raw("sleeper")
    ffc_raw = load_latest_raw("ffc")
    ffc_rows = parse_adp_rows(ffc_raw)
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)
    statlines = to_statlines(load_latest_raw("sleeper_projections"))

    bonus_schedule = load_bonus_schedule() if with_bonus else None
    bonus_curves = load_curves() if with_bonus else None

    seasons: list[PlayerSeason] = []
    for row in ffc_rows:
        pos = (row.pos or "").strip().upper()
        if pos not in SKILL_POSITIONS:
            continue
        key = str(row.player_id) if row.player_id is not None else f"{row.name}|{row.team}|{row.pos}"
        pid = cw.resolve("ffc", key)
        statline = statlines.get(pid) if pid is not None else None
        if pid is None or statline is None or statline.games <= 0:
            continue
        if with_bonus:
            total_points = score_statline_with_bonus(
                statline.as_dict(), cfg.scoring, pos=pos, games=statline.games,
                bonus_schedule=bonus_schedule, bonus_curves=bonus_curves,
            )
        else:
            total_points = score_statline(statline.as_dict(), cfg.scoring)
        seasons.append(
            PlayerSeason(
                player_id=str(pid), pos=pos, ppg=total_points / statline.games,
                expected_games=statline.games, name=row.name,
            )
        )
    return seasons


def report_for(label: str, seasons, cfg: LeagueConfig, pos_of_interest: str):
    values = compute_draft_values(seasons, cfg)
    ordered = sorted(values.values(), key=lambda v: -v.dv)
    top30 = ordered[:30]
    mix = {p: sum(1 for v in top30 if v.pos == p) for p in sorted({v.pos for v in ordered})}
    top_p = next(v for v in ordered if v.pos == pos_of_interest)
    top_p_rank = next(i for i, v in enumerate(ordered, 1) if v.pos == pos_of_interest)
    n_in_top30 = mix.get(pos_of_interest, 0)

    levels = replacement_levels(seasons, cfg)[pos_of_interest]
    top_ppg = max(s.ppg for s in seasons if s.pos == pos_of_interest)
    spread = top_ppg - levels.baseline_ppg

    print(f"--- {label}: {pos_of_interest} ---")
    print(f"  top-30 position mix: {mix}")
    print(f"  {pos_of_interest} in top 30: {n_in_top30}")
    print(
        f"  top {pos_of_interest} ({top_p.name}): dv={top_p.dv:.1f}, ppg={top_p.ppg:.2f}, "
        f"overall rank #{top_p_rank}"
    )
    print(
        f"  {pos_of_interest} baseline: rank{levels.baseline_rank} at {levels.baseline_ppg:.2f} ppg; "
        f"spread top-{pos_of_interest} to baseline = {spread:.2f} ppg"
    )
    return {"mix": mix, "n_in_top30": n_in_top30, "top_dv": top_p.dv, "top_rank": top_p_rank,
            "baseline_ppg": levels.baseline_ppg, "top_ppg": top_ppg, "spread": spread}


def main() -> int:
    logging.getLogger("draftroom.prep.crosswalk").setLevel(logging.ERROR)
    cfg = LeagueConfig.from_yaml()

    print("=" * 100)
    print("BONUS-MODEL EFFECT ON qb_count_in_top30 -- read-only, throwaway comparison")
    print("=" * 100)
    print(f"league: {cfg.teams} teams, starters={dict(cfg.starters)}, scoring pass_int={cfg.scoring['pass_int']}")

    seasons_plain = build_seasons(cfg, with_bonus=False)
    seasons_bonus = build_seasons(cfg, with_bonus=True)
    print(f"\nplayers valued: {len(seasons_plain)} (no-bonus), {len(seasons_bonus)} (with-bonus) -- same join, must match")
    assert len(seasons_plain) == len(seasons_bonus)

    print()
    qb_plain = report_for("NO BONUS", seasons_plain, cfg, "QB")
    print()
    qb_bonus = report_for("WITH BONUS", seasons_bonus, cfg, "QB")

    print()
    rb_plain = report_for("NO BONUS", seasons_plain, cfg, "RB")
    print()
    rb_bonus = report_for("WITH BONUS", seasons_bonus, cfg, "RB")

    print("\n" + "=" * 100)
    print("SIDE BY SIDE")
    print("=" * 100)
    print(f"{'metric':38s} {'QB no-bonus':>14s} {'QB with-bonus':>14s} {'RB no-bonus':>14s} {'RB with-bonus':>14s}")
    print(
        f"{'count in top 30':38s} {qb_plain['n_in_top30']:>14d} {qb_bonus['n_in_top30']:>14d} "
        f"{rb_plain['n_in_top30']:>14d} {rb_bonus['n_in_top30']:>14d}"
    )
    print(
        f"{'top dv':38s} {qb_plain['top_dv']:>14.1f} {qb_bonus['top_dv']:>14.1f} "
        f"{rb_plain['top_dv']:>14.1f} {rb_bonus['top_dv']:>14.1f}"
    )
    print(
        f"{'top overall rank':38s} {qb_plain['top_rank']:>14d} {qb_bonus['top_rank']:>14d} "
        f"{rb_plain['top_rank']:>14d} {rb_bonus['top_rank']:>14d}"
    )
    print(
        f"{'ppg spread (top to baseline)':38s} {qb_plain['spread']:>14.2f} {qb_bonus['spread']:>14.2f} "
        f"{rb_plain['spread']:>14.2f} {rb_bonus['spread']:>14.2f}"
    )

    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    n_qb = qb_bonus["n_in_top30"]
    if 10 <= n_qb <= 14:
        verdict = f"qb_count_in_top30 now PASSES with bonuses on ({n_qb} QBs in top 30)."
    elif n_qb > qb_plain["n_in_top30"]:
        verdict = (
            f"still FAILS ({n_qb} QBs in top 30, need 10-14), but MOVED in the right direction "
            f"from {qb_plain['n_in_top30']} without bonuses."
        )
    else:
        verdict = f"still FAILS and did NOT move toward 10-14 ({qb_plain['n_in_top30']} -> {n_qb})."
    print(verdict)

    qb_spread_gain = qb_bonus["spread"] - qb_plain["spread"]
    rb_spread_gain = rb_bonus["spread"] - rb_plain["spread"]
    print(
        f"\nQB top-to-baseline spread gained {qb_spread_gain:+.2f} ppg from the bonus; "
        f"RB gained {rb_spread_gain:+.2f} ppg over the same change. "
        + (
            "RB gained AT LEAST AS MUCH as QB -- the relative picture (QB vs RB value) barely "
            "moves even though both went up."
            if rb_spread_gain >= qb_spread_gain
            else "QB gained more than RB -- the bonus does shift value specifically toward QB."
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
