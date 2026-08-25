"""Who to research before draft night, computed rather than remembered.

THE PROBLEM THIS SOLVES (Marc, 2026-08-25): the injury picture is the fastest-decaying field in
this pipeline, and the highest-density window for the news that matters -- final roster cuts,
reserve/PUP conversions, IR designations -- runs from the 53-man cutdown right up to kickoff. So
the availability research is not a one-time task that got done once in August. It is a step in
FINAL PREP that must be repeatable by someone who was not in the room when it was first done.

``tools/injury_sweep.py`` decides what to DO with an answer. This decides WHO TO ASK ABOUT, and
it does so from the data rather than from anybody's memory. Four categories, each of which
catches a failure the others miss:

  A. DESIGNATED. Every ranked player carrying an injury designation. The obvious list, and the
     one a human would write down unaided.
  B. SOURCE-IMPLIED, UNDESIGNATED. A player with no designation at all whose games figure was
     nonetheless cut below the healthy-rank curve. Somebody at ESPN knows something the
     designation feed has not said out loud -- ESPN is the only source with a games figure, so
     it is the only source that can leak this, and a discount with no designation behind it is
     precisely the shape of news arriving early.
  C. ADP MOVERS. The market is a source we already pay nothing for and have never read. Every
     FFC pull is cached, so the change in a player's ADP between two snapshots is free, and a
     player falling hard is a player the rest of the internet knows something about. Verified on
     the 2026-08-14 -> 2026-08-25 pair: Jordyn Tyson fell 11.9 spots, which is the market
     pricing a hamstring our own designation feed still called merely "Doubtful".
  D. BLIND TOP-N. The designation feed itself lags, so working only from categories A-C inherits
     its blind spot. The expensive error is a first-five-rounds pick who is actually out, so the
     top of the board gets checked whether or not anything flagged it.

NO CATEGORY USES A THRESHOLD. A and B are membership tests (does he carry a designation; is his
games figure below the curve). C and D are RANKINGS shown to a stated depth, not cutoffs -- the
depth is a CLI argument and is printed in the output, so a shallow run is visible as a shallow
run rather than passing for a clean sweep. That distinction is the whole reason there is no
"significance" number anywhere in this file.

PREP-PHASE ONLY. Reads cached data, writes nothing but its own report.

Usage:
    python tools/injury_worklist.py                    # the worklist
    python tools/injury_worklist.py --prompts          # + ready-to-paste research prompts
    python tools/injury_worklist.py --top 60 --movers 15
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from draftroom import live_data  # noqa: E402
from draftroom.valuation.candidates import (  # noqa: E402
    effective_games_by_pid,
    load_review_inputs,
)

FFC_RAW = REPO_ROOT / "data" / "raw" / "ffc"

#: How deep the two RANKED categories go. Defaults are round numbers and are stated in the
#: output for exactly that reason: they bound coverage, so a run must say what it did not look
#: at rather than let a shallow sweep read as "nothing found".
DEFAULT_TOP = 60
DEFAULT_MOVERS = 15


@dataclass
class Entry:
    """One player to research, and every reason he is on the list."""

    name: str
    pos: str
    team: str
    adp: float | None
    value: float | None
    designation: str | None = None
    reasons: list[str] = field(default_factory=list)
    #: Sleeper player_id -- the crosswalk key injury_research.json and playing_time.json use.
    #: NOT PoolPlayer.player_id, which is FFC-derived and a DIFFERENT number for the same man.
    sleeper_pid: str | None = None


def _sleeper_pids() -> Mapping[tuple[str, str], str]:
    """(normalized name, position) -> Sleeper player_id, for the skill-position universe.

    The research file keys on the Sleeper id, and getting this wrong is silent: an override
    written against the FFC id binds to whichever unrelated player happens to hold that id in
    Sleeper's space and then does nothing at all. So the worklist carries the right id rather
    than leaving the next person to look it up -- and "look it up by hand" is precisely the step
    that gets skipped the night before a draft.

    Joined on ``prep.schema.normalize_name`` rather than the raw string, because the two feeds
    disagree about generational suffixes: Sleeper stores "Luther Burden" and "Michael Pittman"
    where FFC has "Luther Burden III" and "Michael Pittman Jr.". Reusing the pipeline's own
    normalizer means this join can never drift from the crosswalk's.
    """
    from draftroom.prep.http import load_latest_raw
    from draftroom.prep.schema import normalize_name

    out: dict[tuple[str, str], str] = {}
    for pid, row in load_latest_raw("sleeper").items():
        if not isinstance(row, Mapping):
            continue
        name, pos = row.get("full_name"), row.get("position")
        if name and pos in ("QB", "RB", "WR", "TE"):
            out.setdefault((normalize_name(str(name)), str(pos)), str(pid))
    return out


def _pid_key(name: str, pos: str) -> tuple[str, str]:
    """The key into :func:`_sleeper_pids`. One place, so the two sides cannot diverge."""
    from draftroom.prep.schema import normalize_name

    return (normalize_name(name), pos)


def _adp_movers(
    limit: int, raw_dir: Path | None = None
) -> tuple[list[tuple[float, str, str, float, float]], str]:
    """Biggest ADP changes between the two newest cached FFC pulls. Free signal, never read.

    ``raw_dir`` is injectable so this is testable against fixture snapshots rather than
    whatever the live cache happens to hold today.
    """
    snaps = sorted((raw_dir or FFC_RAW).glob("*.json"))
    if len(snaps) < 2:
        return [], "only one ADP snapshot cached -- no movement computable yet"

    def adps(path: Path) -> dict[tuple[str, str], float]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("players") if isinstance(payload, Mapping) else payload
        out: dict[tuple[str, str], float] = {}
        for row in rows or ():
            adp = row.get("adp")
            if row.get("name") and isinstance(adp, (int, float)):
                out[(str(row["name"]), str(row.get("position")))] = float(adp)
        return out

    old, new = adps(snaps[-2]), adps(snaps[-1])
    moves = [
        (new[k] - old[k], k[0], k[1], old[k], new[k])
        for k in set(old) & set(new)
        # Kickers and defenses are not drafted in this league, so they are noise here.
        if k[1] in ("QB", "RB", "WR", "TE")
    ]
    moves.sort(key=lambda m: -abs(m[0]))
    label = f"{snaps[-2].stem[:10]} -> {snaps[-1].stem[:10]}"
    return moves[:limit], label


def build(top: int = DEFAULT_TOP, movers: int = DEFAULT_MOVERS) -> tuple[list[Entry], list[str]]:
    """The worklist, plus the coverage notes that say what it did NOT look at."""
    pool = [p for p in live_data.load_player_pool() if p.is_ranked]
    pool.sort(key=lambda p: p.adp if p.adp is not None else 9_999)
    pids = _sleeper_pids()
    by_key: dict[tuple[str, str], Entry] = {}
    notes: list[str] = []

    def entry_for(player) -> Entry:  # noqa: ANN001
        key = (player.name, player.pos)
        if key not in by_key:
            by_key[key] = Entry(
                name=player.name,
                pos=player.pos,
                team=player.team,
                adp=player.adp,
                value=player.value,
                designation=player.injury_status,
                sleeper_pid=pids.get(_pid_key(player.name, player.pos)),
            )
        return by_key[key]

    # --- A. designated -----------------------------------------------------------------
    for player in pool:
        if player.injury_status:
            entry_for(player).reasons.append(f"A: carries designation {player.injury_status}")

    # --- B. source-implied discount with no designation ---------------------------------
    inputs = load_review_inputs()
    effective = effective_games_by_pid(inputs)
    name_of = inputs.name_of
    pos_of = inputs.pos_of
    by_name_pos = {(p.name, p.pos): p for p in pool}
    for pid, (credited, curve, _rank) in effective.items():
        if credited is None or curve is None or credited >= curve - 1e-9:
            continue
        key = (name_of.get(pid, ""), pos_of.get(pid, ""))
        player = by_name_pos.get(key)
        if player is None:
            continue
        if player.injury_status:
            continue  # already in A; a second reason adds nothing
        entry_for(player).reasons.append(
            f"B: games cut to {credited:.2f} vs a healthy-rank curve of {curve:.2f}, "
            "with NO designation behind it"
        )

    # --- C. ADP movers ------------------------------------------------------------------
    moves, span = _adp_movers(movers)
    for delta, name, pos, before, after in moves:
        player = by_name_pos.get((name, pos))
        direction = "falling" if delta > 0 else "rising"
        reason = f"C: ADP {direction} {abs(delta):.1f} ({before:.1f} -> {after:.1f}) over {span}"
        if player is not None:
            entry_for(player).reasons.append(reason)
        else:
            by_key[(name, pos)] = Entry(
                name=name,
                pos=pos,
                team="?",
                adp=after,
                value=None,
                sleeper_pid=pids.get(_pid_key(name, pos)),
                reasons=[reason + "  [NOT on the valued board]"],
            )
    if moves:
        notes.append(
            f"C: showing the {len(moves)} largest ADP moves over {span}, ranked by absolute "
            "change. This is a DEPTH, not a threshold -- moves outside the top "
            f"{movers} exist and were not listed."
        )
    else:
        notes.append(f"C: {span}")

    # --- D. blind top-N -----------------------------------------------------------------
    for player in pool[:top]:
        entry = entry_for(player)
        if not entry.reasons:
            entry.reasons.append(f"D: top-{top} by ADP, checked blind (the designation feed lags)")
    notes.append(
        f"D: the blind check covers the top {top} by ADP. Players outside it with no "
        "designation, no source-implied cut and no ADP move were NOT examined."
    )

    entries = sorted(by_key.values(), key=lambda e: e.adp if e.adp is not None else 9_999)
    return entries, notes


def render(entries: Sequence[Entry], notes: Sequence[str], top: int, movers: int) -> str:
    out: list[str] = []
    out.append("=" * 96)
    out.append("INJURY / AVAILABILITY RESEARCH WORKLIST")
    out.append("=" * 96)
    by_cat: dict[str, int] = {}
    for e in entries:
        for r in e.reasons:
            by_cat[r[0]] = by_cat.get(r[0], 0) + 1
    out.append(
        f"{len(entries)} players to research  |  reasons: "
        + ", ".join(f"{k}={v}" for k, v in sorted(by_cat.items()))
    )
    out.append("")
    for label, text in (
        ("A", "carries an injury designation"),
        ("B", "games cut below the curve with NO designation -- news arriving early"),
        ("C", "ADP moving hard -- the market knows something"),
        ("D", f"top {top} by ADP, checked blind because the designation feed itself lags"),
    ):
        out.append(f"  {label} = {text}")
    out.append("")
    for note in notes:
        out.append(f"  COVERAGE: {note}")
    out.append("")
    out.append("-" * 96)
    for e in entries:
        pid = e.sleeper_pid or "PID NOT FOUND"
        val = "n/a" if e.value is None else f"{e.value:7.1f}"
        adp = "unranked" if e.adp is None else f"{e.adp:6.1f}"
        out.append(f"{e.name:26} {e.pos:3} {e.team:4} adp {adp}  dv {val}  sleeper_pid={pid}")
        for r in e.reasons:
            out.append(f"      {r}")
    out.append("-" * 96)
    return "\n".join(out)


PROMPT_TEMPLATE = """\
You are researching CURRENT NFL injury and availability status for a fantasy football draft on
{draft_date}. Today is {today}. The season is {weeks} weeks. Accuracy matters more than
completeness: a wrong "he's fine" costs a real pick, and a wrong "he's hurt" wastes one.

Use WebSearch and WebFetch for the MOST RECENT reporting (last 7 days) on each player below.

PLAYERS:
{players}

For EACH player report EXACTLY this structure:

PLAYER: <name>
CURRENT STATUS: <healthy / Questionable / Doubtful / Out / IR / PUP / suspended / cut / unknown>
INJURY: <what is wrong, few words, or "none reported">
SEASON-ENDING: <YES / NO / UNCLEAR>
EXPECTED RETURN: <week number, date, "week 1", "unknown", or "no return expected">
GAMES LIKELY MISSED of {weeks}: <number or range; 0 if he plays a full season>
CONFIDENCE: <HIGH / MEDIUM / LOW>
MOST RECENT REPORT: <date, publication, URL>
NOTES: <practice participation, a reported timeline, whether a tag is just a rest day>

CRITICAL RULES:
- If you find nothing recent, say "NO RECENT REPORTING FOUND" and set CONFIDENCE: LOW. Do NOT
  guess or fill in from training knowledge, which is out of date. "We don't know" is useful.
- Every status claim must trace to something you actually fetched, with a date.
- Many preseason "questionable" tags are trivial. Say so when that is what you find:
  over-discounting a healthy player is its own expensive error.
- Flag prominently any player who has been cut, traded, changed teams, or is a FREE AGENT.
- Flag SUSPENSION and other non-injury availability risk. No projection source prices it.
- Note the 53-man cutdown: active/PUP either clears or becomes reserve/PUP (4-game minimum).

Your final message IS the report. No preamble.
"""


def prompts(entries: Sequence[Entry], *, batch: int, today: str, draft_date: str, weeks: int) -> str:
    """Batched, ready-to-paste research prompts -- one per subagent."""
    out: list[str] = []
    batches = [entries[i : i + batch] for i in range(0, len(entries), batch)]
    for n, group in enumerate(batches, start=1):
        lines = []
        for i, e in enumerate(group, start=1):
            adp = "unranked" if e.adp is None else f"{e.adp:.1f}"
            why = "; ".join(e.reasons)
            lines.append(f"{i}. {e.name}, {e.pos}, {e.team} (ADP {adp}) -- why flagged: {why}")
        out.append("#" * 96)
        out.append(f"# RESEARCH BATCH {n} of {len(batches)}  ({len(group)} players)")
        out.append("#" * 96)
        out.append(
            PROMPT_TEMPLATE.format(
                players="\n".join(lines), today=today, draft_date=draft_date, weeks=weeks
            )
        )
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    from datetime import date

    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="blind-check depth by ADP")
    ap.add_argument("--movers", type=int, default=DEFAULT_MOVERS, help="how many ADP movers")
    ap.add_argument("--prompts", action="store_true", help="also emit research prompts")
    ap.add_argument("--batch", type=int, default=10, help="players per research prompt")
    ap.add_argument("--draft-date", default="2026-09-08")
    ap.add_argument("--weeks", type=int, default=17)
    ap.add_argument("--out", type=Path, default=None, help="write the worklist here too")
    args = ap.parse_args(argv)

    entries, notes = build(top=args.top, movers=args.movers)
    text = render(entries, notes, args.top, args.movers)
    print(text)

    if args.prompts:
        block = prompts(
            entries,
            batch=args.batch,
            today=date.today().isoformat(),
            draft_date=args.draft_date,
            weeks=args.weeks,
        )
        print()
        print(block)
        text = text + "\n\n" + block

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"\nworklist written: {args.out}")

    missing = [e.name for e in entries if not e.sleeper_pid]
    if missing:
        print(
            f"\nWARNING: no Sleeper player_id resolved for {len(missing)}: {', '.join(missing)}. "
            "Research them by name, but look the id up before writing any override -- an "
            "override against the wrong id fails silently."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
