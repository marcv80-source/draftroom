r"""Is FantasySharks a FOURTH source, or a re-publication of one we already have?

This check is a PRECONDITION for using FantasySharks in anything -- the composite, the
disagreement badge, the team-envelope validator, the review queue. Nothing may consume it until
this has run and said "independent".

WHY THIS TOOL EXISTS, IN ONE PARAGRAPH. CLAUDE.md records the exact mistake a fourth source
invites: ESPN's API and the Mike Clay draft-kit PDF looked like two sources and were one --
411 of 411 overlapping players agreed on every stat, max difference 0.50, because the PDF is
Clay's numbers rounded and the API is the same numbers with decimals. Counting them as two made
cross-source disagreement look artificially small, which is the worst possible failure for a
tool whose whole job is to surface disagreement. A fourth source that quietly re-publishes
Sleeper, ESPN or FantasyPros would do the same damage, and it would look like progress.

WHAT IT MEASURES. For every canonical component stat that FantasySharks and the other source
BOTH publish at that player's position, over the players both resolve onto the same crosswalk
pid: how many players agree exactly, how many agree within rounding, the max and mean absolute
difference, the mean absolute difference as a share of the stat's own mean, and the Pearson
correlation. Then the headline number the ESPN/Clay finding was stated in: what share of
overlapping players agree on EVERY compared stat.

WHY THERE ARE TWO CONTROL PAIRS. A correlation of 0.97 means nothing in isolation -- every
projection source is forecasting the same football season, so they all correlate highly. The
report therefore also runs the identical machinery over:

  * ESPN vs Mike Clay PDF  -- the KNOWN re-publication (a positive control). This is what
                              "same source twice" looks like in these exact numbers.
  * Sleeper vs ESPN        -- two sources CLAUDE.md already accepts as independent families
                              (a negative control). This is what "genuinely different" looks
                              like.

FantasySharks' numbers are read against those two, not against an invented threshold.

ALSO REPORTED, because they are the other things that have to be true before this source is
usable: the full field mapping, the player counts, the crosswalk resolution rate (overall and
against the top 200 by ADP -- CLAUDE.md gate #2), the measured ``games`` situation, and which of
this league's per-game yardage bonus tiers the threshold columns actually cover.

Reads only cached files under ``data/raw/`` and ``data/manual/`` unless ``--fetch`` is passed.
Never run ``prep/fetch_all.py`` to "refresh" for this: CLAUDE.md documents that it moves what
``load_latest_raw()`` resolves to and breaks unrelated tests. ``--fetch`` here writes only into
``data/raw/fantasysharks/``, a directory no other source reads.

Run:
    C:\dev\draftroom\.venv\Scripts\python.exe tools\verify_fantasysharks.py
    ... --fetch          pull fresh pages first (writes data/raw/fantasysharks/ only)
    ... --top-adp 200    size of the ADP window the completeness gate is measured over
    ... --misses         list every top-ADP player FantasySharks does not cover
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from draftroom.prep import clay_pdf, espn_client, fantasysharks_client as fsc  # noqa: E402
from draftroom.prep.crosswalk import DYNASTYPROCESS_SOURCE, build_crosswalk  # noqa: E402
from draftroom.prep.ffc_client import parse_adp_rows  # noqa: E402
from draftroom.prep.http import load_latest_raw  # noqa: E402
from draftroom.prep.schema import CANONICAL_STATS, StatLine, normalize_name  # noqa: E402
from draftroom.prep.sleeper_client import filter_active_skill_players  # noqa: E402
from draftroom.valuation import composite  # noqa: E402
from draftroom.valuation.bonuses import load_bonus_schedule  # noqa: E402

SEASON = 2026

#: "The same number." Two sources printing 131.8 and 131.8 agree exactly.
EXACT_TOL = 0.005

#: "The same number, one of them rounded." This is the ESPN/Clay signature, and the value is
#: theirs: max difference 0.50 across 411 players on every stat, because a half-unit is the most
#: integer rounding can move a figure. Anything inside this band on YARDAGE-scale numbers is not
#: two forecasts agreeing, it is one forecast printed twice.
ROUND_TOL = 0.5

#: Comparing structural zeros would drown the signal: a QB's ``rec_yd`` is 0.0 everywhere in
#: every source, and counting those as agreement drives every exact-match rate toward 100%.
#: A pair is compared only if at least one side exceeds this.
NONZERO_FLOOR = 1e-9


# --------------------------------------------------------------------------- loading


@dataclass
class Loaded:
    statlines: dict[str, dict[str, StatLine]]  # source -> pid -> StatLine
    pos_of: dict[str, str]
    name_of: dict[str, str]
    team_of: dict[str, str]
    adp_of: dict[str, float]
    fs_rows: list[fsc.FantasySharksRow]
    fs_payload: dict
    fs_resolution: dict[str, int]
    fs_unresolved: list[tuple[str, str, str]]  # (name, team, pos)
    #: normalized name -> Sleeper's own position, over the UNFILTERED Sleeper universe. The
    #: crosswalk spine is filtered to QB/RB/WR/TE, so a player Sleeper calls anything else (FB,
    #: for instance) is structurally unjoinable rather than a crosswalk defect -- and that is a
    #: different finding, so the report has to be able to tell them apart.
    sleeper_pos_by_name: dict[str, str]


def load_everything(*, fetch: bool) -> Loaded:
    sleeper_raw = load_latest_raw("sleeper")
    ffc_rows = list(parse_adp_rows(load_latest_raw("ffc")))
    try:
        dp_csv = load_latest_raw(DYNASTYPROCESS_SOURCE)
    except FileNotFoundError:
        dp_csv = None
        print("WARNING: no cached DynastyProcess crosswalk CSV; stage-1 direct-ID matching runs "
              "on Sleeper's own cross-IDs only.")
    cw = build_crosswalk(sleeper_raw, ffc_rows, dynastyprocess_csv_text=dp_csv)

    sleeper_pos_by_name = {
        normalize_name(f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()):
            (p.get("position") or "")
        for p in sleeper_raw.values()
        if p and (p.get("first_name") or p.get("last_name"))
    }

    universe = filter_active_skill_players(sleeper_raw)
    pos_of = {pid: ref.pos.upper() for pid, ref in universe.items()}
    name_of = {pid: ref.name for pid, ref in universe.items()}
    team_of = {pid: ref.team for pid, ref in universe.items()}

    # ADP per pid, straight off the FFC entries the crosswalk just resolved.
    adp_of: dict[str, float] = {}
    for (source, _key), entry in cw.entries.items():
        if source == "ffc" and entry.pid is not None and entry.adp is not None:
            adp_of[entry.pid] = min(entry.adp, adp_of.get(entry.pid, entry.adp))

    # The three incumbent sources, joined exactly the way validate/board.py joins them.
    from draftroom.prep.sleeper_client import to_statlines as sleeper_to_statlines
    from draftroom.validate.board import (
        _resolve_espn_statlines,
        _resolve_fantasypros_statlines,
    )

    statlines: dict[str, dict[str, StatLine]] = {
        "sleeper": {
            pid: line
            for pid, line in sleeper_to_statlines(load_latest_raw("sleeper_projections")).items()
            if line.has_nonzero_stats()
        },
        "espn": _resolve_espn_statlines(cw),
        "fantasypros": _resolve_fantasypros_statlines(cw),
    }

    # FantasySharks.
    if fetch:
        fs_payload = fsc.fetch_projections(SEASON)
    else:
        fs_payload = fsc.load_cached()
    fs_rows = fsc.parse_all(fsc.pages_of(fs_payload))

    fs_by_pid: dict[str, StatLine] = {}
    fs_unresolved: list[tuple[str, str, str]] = []
    methods: dict[str, int] = {}
    for row in fs_rows:
        entry = cw.resolve_fantasysharks_row(row.source_key, row.name, row.team, row.pos)
        methods[entry.resolve_method] = methods.get(entry.resolve_method, 0) + 1
        if entry.pid is None:
            fs_unresolved.append((row.name, row.team, row.pos))
        else:
            fs_by_pid[entry.pid] = row.stats
    statlines["fantasysharks"] = fs_by_pid

    return Loaded(
        statlines=statlines, pos_of=pos_of, name_of=name_of, team_of=team_of, adp_of=adp_of,
        fs_rows=fs_rows, fs_payload=fs_payload, fs_resolution=methods,
        fs_unresolved=fs_unresolved, sleeper_pos_by_name=sleeper_pos_by_name,
    )


# --------------------------------------------------------------------------- comparison


@dataclass
class StatCompare:
    stat: str
    n: int
    n_exact: int
    n_within_round: int
    max_abs: float
    mean_abs: float
    mean_level: float
    corr: float | None

    @property
    def rel_mean_abs(self) -> float:
        return self.mean_abs / self.mean_level if self.mean_level else 0.0


@dataclass
class PairCompare:
    a: str
    b: str
    stats: list[StatCompare]
    players_overlapping: int
    players_all_exact: int
    players_all_within_round: int


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None  # one side is constant -- correlation undefined, not zero


def _published(source: str, pos: str) -> frozenset[str]:
    if source == "fantasysharks":
        return fsc.PUBLISHED_STATS_BY_POS.get(pos, frozenset())
    if source == "clay_pdf":
        # Clay's PDF tables carry a NARROWER column set than the ESPN API: no two-point
        # conversions and no fumbles-lost column at all (``clay_pdf.CANONICAL_FIELD_MAP``).
        # Using ESPN's set here instead would compare 2pt and fum_lost against structural
        # zeros and drag the positive control down to ~68% -- which is exactly what it did
        # before this was fixed, and would have mis-calibrated the whole report.
        return frozenset(
            c for c in clay_pdf.CANONICAL_FIELD_MAP.values() if c is not None
        )
    return composite.published_stats(source, pos)


def compare_pair(
    a: str, b: str,
    lines: dict[str, dict[str, StatLine]],
    pos_of: dict[str, str],
    published: Callable[[str, str], frozenset[str]] = _published,
) -> PairCompare:
    """Field-by-field comparison of two sources over the players both resolve to.

    ``published`` is injectable so the ESPN-vs-Clay control can compare two *aliases* of real
    sources without either of them needing an entry in the real published-stats tables.
    """
    la, lb = lines.get(a, {}), lines.get(b, {})
    shared_pids = sorted(set(la) & set(lb))

    per_stat_pairs: dict[str, list[tuple[float, float]]] = {}
    per_player: dict[str, list[float]] = {}
    for pid in shared_pids:
        pos = pos_of.get(pid, "")
        both = published(a, pos) & published(b, pos)
        da, db = la[pid].as_dict(), lb[pid].as_dict()
        for stat in CANONICAL_STATS:
            if stat not in both:
                continue
            va, vb = float(da[stat]), float(db[stat])
            if abs(va) <= NONZERO_FLOOR and abs(vb) <= NONZERO_FLOOR:
                continue
            per_stat_pairs.setdefault(stat, []).append((va, vb))
            per_player.setdefault(pid, []).append(abs(va - vb))

    stats_out: list[StatCompare] = []
    for stat in CANONICAL_STATS:
        pairs = per_stat_pairs.get(stat)
        if not pairs:
            continue
        diffs = [abs(x - y) for x, y in pairs]
        levels = [(abs(x) + abs(y)) / 2.0 for x, y in pairs]
        stats_out.append(
            StatCompare(
                stat=stat,
                n=len(pairs),
                n_exact=sum(1 for d in diffs if d <= EXACT_TOL),
                n_within_round=sum(1 for d in diffs if d <= ROUND_TOL),
                max_abs=max(diffs),
                mean_abs=sum(diffs) / len(diffs),
                mean_level=sum(levels) / len(levels),
                corr=_pearson([x for x, _ in pairs], [y for _, y in pairs]),
            )
        )

    compared_players = [d for d in per_player.values() if d]
    return PairCompare(
        a=a, b=b, stats=stats_out,
        players_overlapping=len(compared_players),
        players_all_exact=sum(1 for d in compared_players if max(d) <= EXACT_TOL),
        players_all_within_round=sum(1 for d in compared_players if max(d) <= ROUND_TOL),
    )


def print_pair(pc: PairCompare) -> None:
    print(f"\n-- {pc.a}  vs  {pc.b} --")
    if not pc.stats:
        print("   no comparable stat/player overlap at all.")
        return
    print(f"   {'stat':<10} {'n':>5} {'exact':>7} {'<=0.5':>7} {'max|d|':>10} "
          f"{'mean|d|':>9} {'mean|d|/lvl':>12} {'corr':>7}")
    for s in pc.stats:
        corr = f"{s.corr:.4f}" if s.corr is not None else "  n/a"
        print(f"   {s.stat:<10} {s.n:>5} {s.n_exact:>6}  {s.n_within_round:>6}  "
              f"{s.max_abs:>10.3f} {s.mean_abs:>9.3f} {s.rel_mean_abs:>11.1%} {corr:>7}")
    tot = pc.players_overlapping
    if tot:
        print(f"   players compared: {tot}   agreeing on EVERY stat exactly: "
              f"{pc.players_all_exact} ({pc.players_all_exact / tot:.1%})   "
              f"within rounding (<= {ROUND_TOL}): {pc.players_all_within_round} "
              f"({pc.players_all_within_round / tot:.1%})")


def verdict(pc: PairCompare) -> tuple[str, str]:
    """``(verdict, reasoning)`` for one pair, from the numbers only.

    The rule is stated rather than tuned: a re-publication is a pair where essentially every
    overlapping player matches on essentially every stat once rounding is allowed. The
    ESPN/Clay control is what calibrates "essentially" -- it scores 100%.
    """
    tot = pc.players_overlapping
    if not tot:
        return "NO OVERLAP", "nothing comparable; cannot judge"
    share_round = pc.players_all_within_round / tot
    corrs = [s.corr for s in pc.stats if s.corr is not None]
    med_corr = statistics.median(corrs) if corrs else float("nan")
    med_rel = statistics.median([s.rel_mean_abs for s in pc.stats]) if pc.stats else float("nan")
    if share_round >= 0.95:
        return (
            "RE-PUBLICATION",
            f"{share_round:.1%} of {tot} overlapping players match on every compared stat "
            f"within rounding -- this is one source printed twice, not two forecasts",
        )
    return (
        "INDEPENDENT",
        f"only {share_round:.1%} of {tot} overlapping players match on every stat within "
        f"rounding; median per-stat mean|d| is {med_rel:.1%} of the stat's own level, median "
        f"correlation {med_corr:.3f}",
    )


# --------------------------------------------------------------------------- report sections


def print_mapping() -> None:
    print("\n" + "=" * 100)
    print("FIELD MAPPING  (prep/fantasysharks_client.POSITION_LAYOUTS -- read positionally)")
    print("=" * 100)
    for pos, layout in fsc.POSITION_LAYOUTS.items():
        print(f"\n  {pos}  ({len(layout)} columns)")
        for idx, spec in enumerate(layout):
            if spec.canonical:
                target = f"-> {spec.canonical}"
            elif spec.threshold:
                target = f"-> threshold {spec.threshold[0]} >= {spec.threshold[1]:g} yd"
            else:
                target = "-- NOT MAPPED"
            print(f"    {idx:>2}  {spec.header:<16} {target}")
            if not spec.canonical and not spec.threshold:
                print(f"        reason: {spec.note}")
    print("\n  canonical stats published, per position:")
    for pos, published in fsc.PUBLISHED_STATS_BY_POS.items():
        missing = sorted(set(CANONICAL_STATS) - published)
        print(f"    {pos}: {len(published)} published -> {sorted(published)}")
        print(f"        NOT published (structural, must never be averaged as zero): {missing}")


def print_counts(loaded: Loaded, top_adp: int, show_misses: bool) -> None:
    rows = loaded.fs_rows
    by_pos: dict[str, int] = {}
    for r in rows:
        by_pos[r.pos] = by_pos.get(r.pos, 0) + 1

    print("\n" + "=" * 100)
    print("PLAYER AND RESOLUTION COUNTS")
    print("=" * 100)
    p = loaded.fs_payload
    print(f"  season={p.get('season')}  segment={p.get('segment')} "
          f"({p.get('segment_label')!r})  scoring={p.get('scoring')}  "
          f"fetched={p.get('fetched_utc')}")
    print(f"  players published: {len(rows)}  " +
          "  ".join(f"{k} {v}" for k, v in sorted(by_pos.items())))
    print(f"  rookies flagged via <sup>R</sup>: {sum(1 for r in rows if r.rookie)}")
    print(f"  players with a projected rec_tgt > 0: "
          f"{sum(1 for r in rows if r.stats.rec_tgt > 0)}")

    print("\n  crosswalk resolution (prep/crosswalk.resolve_fantasysharks_row):")
    total = sum(loaded.fs_resolution.values())
    for method, n in sorted(loaded.fs_resolution.items(), key=lambda kv: -kv[1]):
        print(f"    {method:<24} {n:>4}  ({n / total:.1%})")
    resolved = total - loaded.fs_resolution.get("unresolved", 0)
    print(f"    RESOLVED {resolved} of {total} ({resolved / total:.1%})")

    # CLAUDE.md gate #2 is stated over the top N by ADP, so measure it there.
    ranked = sorted(loaded.adp_of.items(), key=lambda kv: kv[1])[:top_adp]
    fs_pids = set(loaded.statlines["fantasysharks"])
    covered = [pid for pid, _ in ranked if pid in fs_pids]
    misses = [(pid, adp) for pid, adp in ranked if pid not in fs_pids]
    print(f"\n  top {top_adp} by FFC ADP (resolved onto the crosswalk): {len(ranked)} players")
    print(f"    covered by FantasySharks : {len(covered)} ({len(covered) / len(ranked):.1%})")
    print(f"    NOT covered              : {len(misses)}")

    # An uncovered top-ADP player is either "FantasySharks never published them" or "their row
    # IS published and failed to resolve". Those are completely different problems -- the first
    # is a coverage limit, the second is a CLAUDE.md gate #2 failure with a one-line fix in
    # data/overrides.csv -- so they must not be reported as one number.
    #
    # Matching the two lists on an exact normalized name would miss the very cases that cause
    # join failures in the first place: a nickname the fold table does not carry (FantasySharks'
    # "Kenneth Gainwell" vs Sleeper's "Kenny Gainwell" scored 86.7 against a 90 threshold). So
    # the pairing here is deliberately LOOSER than the crosswalk's own matcher -- surname plus
    # position, then fuzzy -- because its job is to explain a miss, not to make a join.
    join_failures: list[tuple[str, float, tuple[str, str, str]]] = []
    not_published: list[tuple[str, float]] = []
    for pid, adp in misses:
        board_name = loaded.name_of.get(pid, "")
        pos = loaded.pos_of.get(pid, "")
        surname = normalize_name(board_name).split(" ")[-1] if board_name else ""
        candidate = next(
            (
                (n, t, ps) for n, t, ps in loaded.fs_unresolved
                if ps == pos
                and surname
                and normalize_name(n).split(" ")[-1] == surname
            ),
            None,
        )
        if candidate is not None:
            join_failures.append((pid, adp, candidate))
        else:
            not_published.append((pid, adp))
    print(f"      of which a FAILED CROSSWALK JOIN (the gate-relevant kind): "
          f"{len(join_failures)}")
    print(f"      of which FantasySharks simply does not publish the player: "
          f"{len(not_published)}")
    if join_failures:
        print("      JOIN FAILURES INSIDE THE ADP WINDOW -- gate #2 failures, fix in "
              "data/overrides.csv:")
        for pid, adp, (fs_name, fs_team, fs_pos) in join_failures:
            print(f"        adp {adp:>6.1f}  board: {loaded.name_of.get(pid, '?')} "
                  f"({loaded.pos_of.get(pid, '?')}/{loaded.team_of.get(pid, '?')})  "
                  f"<- FantasySharks publishes {fs_name!r} ({fs_pos}/{fs_team}) unresolved")
    if show_misses and not_published:
        print("      not published by FantasySharks:")
        for pid, adp in not_published:
            print(f"        adp {adp:>6.1f}  {loaded.name_of.get(pid, '?')} "
                  f"({loaded.pos_of.get(pid, '?')}/{loaded.team_of.get(pid, '?')})")

    if loaded.fs_unresolved:
        print(f"\n  all unresolved FantasySharks rows ({len(loaded.fs_unresolved)}), any depth,")
        print("  each with the position SLEEPER gives them. The crosswalk spine is filtered to")
        print("  QB/RB/WR/TE, so anything else below is structurally unjoinable -- a scope fact,")
        print("  not a crosswalk defect, and a different finding from a real name mismatch:")
        for name, team, pos in sorted(loaded.fs_unresolved):
            sleeper_pos = loaded.sleeper_pos_by_name.get(normalize_name(name))
            if sleeper_pos and sleeper_pos not in ("QB", "RB", "WR", "TE"):
                why = f"Sleeper calls them {sleeper_pos} -- outside the spine's scope"
            elif sleeper_pos:
                why = f"Sleeper has them as {sleeper_pos} -- a real name/team mismatch"
            else:
                why = "not in the Sleeper universe at all"
            print(f"    {name:<24} ({pos}/{team})   {why}")


def print_games(loaded: Loaded) -> None:
    print("\n" + "=" * 100)
    print("THE `games` COLUMN -- MEASURED, NOT ASSUMED")
    print("=" * 100)
    rep = fsc.games_report(fsc.pages_of(loaded.fs_payload), loaded.fs_rows)
    for pos, info in rep["positions"].items():
        print(f"  {pos}: {len(info['header'])} header columns, games-shaped headers: "
              f"{info['games_headers'] or 'NONE'}")
    print(f"\n  games columns found across all four tables : {rep['games_columns'] or 'NONE'}")
    print(f"  DISTINCT positive `games` values published  : {rep['distinct_values']}  "
          f"{rep['values']}")
    print(f"  (measured over {rep['players_parsed']} parsed players)")

    # The comparison that makes the number mean something.
    counts = composite.games_distinct_counts({
        s: lines for s, lines in loaded.statlines.items() if s in composite.SOURCE_PUBLISHES
    })
    counts["fantasysharks"] = rep["distinct_values"]
    print("\n  distinct positive `games` values, every source, same measurement:")
    for source, n in sorted(counts.items()):
        tag = {0: "no games column at all",
               1: "ONE value = a blanket constant, not a durability forecast"}.get(
            n, "real per-player variation")
        print(f"    {source:<14} {n:>3}   {tag}")
    print(f"\n  {fsc.GAMES_NOTE}")


def print_bonus_coverage() -> None:
    print("\n" + "=" * 100)
    print("PER-GAME YARDAGE THRESHOLD COLUMNS vs THIS LEAGUE'S BONUS TIERS")
    print("=" * 100)
    schedule = load_bonus_schedule()
    coverage = fsc.bonus_tier_coverage(schedule)
    for stat, info in coverage.items():
        tiers = ", ".join(f"{t:g}" for t in info["league_tiers"])
        print(f"\n  {stat}   league tiers: {tiers}")
        for thr in info["league_tiers"]:
            positions = info["covered"].get(thr)
            if positions:
                print(f"    {thr:>6.0f} yd  COVERED   by {', '.join(positions)}")
            else:
                print(f"    {thr:>6.0f} yd  NOT COVERED -- no column at this threshold")
        if info["extra_thresholds"]:
            extra = ", ".join(
                f"{t:g} yd ({', '.join(ps)})" for t, ps in info["extra_thresholds"].items()
            )
            print(f"    extra thresholds published that the league does not pay: {extra}")
            print("      (still useful: they constrain the shape of the same per-game "
                  "distribution valuation/bonuses.py estimates)")


def print_independence(loaded: Loaded) -> None:
    print("\n" + "=" * 100)
    print("INDEPENDENCE CHECK -- FantasySharks vs every source already in the pipeline")
    print("=" * 100)

    lines = dict(loaded.statlines)

    # Positive control: the KNOWN re-publication (CLAUDE.md, 411/411 identical).
    try:
        clay_statlines, _ = clay_pdf.load_or_parse(SEASON)
        espn_by_norm = {}
        espn_raw = load_latest_raw("espn")
        refs = espn_client.to_player_refs(espn_raw.get("players", []), SEASON)
        espn_stats = espn_client.to_statlines(espn_raw.get("players", []), SEASON)
        for espn_id, ref in refs.items():
            if espn_id in espn_stats:
                espn_by_norm[(normalize_name(ref.name), ref.pos.upper())] = espn_stats[espn_id]
        clay_by_norm = {}
        for key, line in clay_statlines.items():
            name, _, _team = key.partition("|")
            for pos in ("QB", "RB", "WR", "TE"):
                if (normalize_name(name), pos) in espn_by_norm:
                    clay_by_norm[(normalize_name(name), pos)] = line
                    break
        # Re-key both onto a synthetic pid so compare_pair's machinery applies unchanged.
        ctrl_lines = {"espn_ctrl": {}, "clay_ctrl": {}}
        ctrl_pos: dict[str, str] = {}
        for i, key in enumerate(sorted(clay_by_norm)):
            pid = f"ctrl{i}"
            ctrl_lines["espn_ctrl"][pid] = espn_by_norm[key]
            ctrl_lines["clay_ctrl"][pid] = clay_by_norm[key]
            ctrl_pos[pid] = key[1]
        ctrl_alias = {"espn_ctrl": "espn", "clay_ctrl": "clay_pdf"}
        pc = compare_pair(
            "espn_ctrl", "clay_ctrl", ctrl_lines, ctrl_pos,
            published=lambda src, pos: _published(ctrl_alias[src], pos),
        )
        print("\n[POSITIVE CONTROL] ESPN API vs Mike Clay PDF -- known to be ONE source")
        print_pair(pc)
        v, why = verdict(pc)
        print(f"   VERDICT: {v} -- {why}")
        if v != "RE-PUBLICATION":
            print("   !! The positive control did NOT reproduce as a re-publication. Treat the "
                  "FantasySharks verdict below as UNCALIBRATED and investigate this first.")
    except Exception as exc:  # noqa: BLE001 - a missing PDF must not block the real check
        print(f"\n[POSITIVE CONTROL] skipped: {type(exc).__name__}: {exc}")
        print("   Without it the thresholds below are uncalibrated -- state that in any writeup.")

    # Negative control: two families CLAUDE.md already accepts as independent.
    pc = compare_pair("sleeper", "espn", lines, loaded.pos_of)
    print("\n[NEGATIVE CONTROL] Sleeper vs ESPN -- accepted as two independent families")
    print_pair(pc)
    v, why = verdict(pc)
    print(f"   VERDICT: {v} -- {why}")

    # The actual question.
    results = {}
    for other in ("sleeper", "espn", "fantasypros"):
        pc = compare_pair("fantasysharks", other, lines, loaded.pos_of)
        print_pair(pc)
        v, why = verdict(pc)
        print(f"   VERDICT: {v} -- {why}")
        results[other] = (v, why)

    print("\n" + "-" * 100)
    dupes = [s for s, (v, _) in results.items() if v == "RE-PUBLICATION"]
    if dupes:
        print(f"OVERALL VERDICT: FantasySharks is a RE-PUBLICATION of {', '.join(dupes)}.")
        print("DO NOT use it as a fourth source. It must not enter the composite, the")
        print("disagreement measure, the envelope validator or the review queue -- doing so")
        print("would repeat the ESPN/Clay error CLAUDE.md documents.")
    else:
        print("OVERALL VERDICT: FantasySharks is INDEPENDENT of Sleeper, ESPN and FantasyPros.")
        print("It is a genuine fourth family and may be wired into the composite.")
    print("-" * 100)


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fetch", action="store_true",
                    help="pull fresh pages (writes only data/raw/fantasysharks/)")
    ap.add_argument("--top-adp", type=int, default=200,
                    help="ADP window for the crosswalk-completeness gate (default 200)")
    ap.add_argument("--misses", action="store_true",
                    help="list every top-ADP player FantasySharks does not publish")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    loaded = load_everything(fetch=args.fetch)

    print_mapping()
    print_counts(loaded, args.top_adp, args.misses)
    print_games(loaded)
    print_bonus_coverage()
    print_independence(loaded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
