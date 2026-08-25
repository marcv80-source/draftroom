"""Cross-check external injury reporting against what every source still publishes.

WHY THIS EXISTS (Marc, 2026-08-25): projection sources lag injury news, and they lag
INDEPENDENTLY. One source can pick up a season-ending injury while the other three keep
publishing a full statline, and the blend then averages a dead player's projection into the
board. The failure we are buying insurance against is drafting a man who is out for the year
because two of four sources had not caught up.

The exposure is worse than "two of four", and the asymmetry is the whole reason this file is
separate from the review queue:

  * AVAILABILITY has NO cross-source redundancy at all. Sleeper reports a blanket 18.0 games
    for every player, and FantasyPros and FantasySharks publish no games column whatsoever
    (``composite.varying_games_sources``). ESPN is the only source with a real per-player games
    figure, so if ESPN lags, nothing downstream notices. There is no second opinion to disagree
    with it.
  * The STATLINE has four sources, any one of which can keep a full season of yardage alive.

Those are two different defects and they take two different levers, both of which already
exist. This module only decides WHICH lever, and it does so by arithmetic:

  1. SEASON-ENDING (or games_missed >= the league's weeks). Availability collapses to zero, so
     ``expected_games = 0`` -- which zeroes EVoB while leaving the player on the board for
     bookkeeping, exactly the semantics ``(PPG - baseline) * expected_games`` already has. And
     any source still publishing a NONZERO statline is describing a season that will not
     happen: that is CONTAMINATION, the one trigger CLAUDE.md admits for rejecting a
     projection, and it needs no distance threshold because "he will play zero games and this
     source projects 1,200 yards" is a failed identity, not an outlier.
  2. PARTIAL ABSENCE (a known return week). Only the VOLUME changed. The per-game rate a source
     projects is still the right rate, so the statline is left alone -- CLAUDE.md: "PPG is never
     touched" by an availability judgement -- and the override carries
     ``weeks - games_missed``, which the fitted curve then clamps as it clamps everything else.
     A source is reported as BEHIND here only when its own published games figure exceeds that
     arithmetic, which in practice can only ever be ESPN, because it is the only source with a
     games figure to be wrong about.

No threshold is invented anywhere in this file. Every "behind" verdict is a comparison between
a number a source published and a number external reporting implies. That matters because the
repo's standing rule is that a correction must beat a dumb null of equal magnitude, and a rule
of the form "reject anything more than X away from the median" could not clear it at four
correlated sources. "This source says he plays, the beat reporter says his season is over" is
not a distance measure.

FAILS CLOSED, the same asymmetry as ``decisions.py`` and ``playing_time.py``:
missing ``data/injury_research.json`` means there is nothing to sweep; a file that exists but
is empty or malformed RAISES, because that is what a truncated write looks like and degrading
would silently stop applying a judgement about a player who is out for the season.

PREP-PHASE ONLY. Reads cached data and writes decision files. Never run on draft night.

Usage:
    python tools/injury_sweep.py                 # report only, changes nothing
    python tools/injury_sweep.py --apply         # write the overrides and rejections
    python tools/injury_sweep.py --json out.json # also dump the machine-readable sweep
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from draftroom.valuation import decisions as dc  # noqa: E402
from draftroom.valuation import playing_time as pt  # noqa: E402
from draftroom.valuation.candidates import (  # noqa: E402
    ImpactEngine,
    ReviewInputs,
    effective_games_by_pid,
    load_review_inputs,
)

#: Where the external research lands. Hand-editable by design, like every other decision file
#: in this repo: a human can read it, correct it, and see the citation that produced it.
RESEARCH_PATH = REPO_ROOT / "data" / "injury_research.json"

#: The detector name stamped on every decision this tool writes, so the audit trail says which
#: mechanism made the call rather than leaving it anonymous.
DETECTOR = "injury_sweep"


class InjuryResearchError(ValueError):
    """The research file exists but cannot be trusted. Never degraded to 'no findings'."""


@dataclass(frozen=True)
class Finding:
    """One player's externally researched status. Mirrors the JSON one-for-one."""

    player_id: str
    player_name: str
    status: str
    season_ending: bool
    games_missed: float
    confidence: str
    report_date: str
    citation: str
    notes: str = ""

    @property
    def is_severe(self) -> bool:
        """Season over, by either the explicit flag or the arithmetic. No threshold here."""
        return self.season_ending


@dataclass
class SourceView:
    """What one source currently publishes about a player, and whether it is behind."""

    source: str
    games: float | None
    #: Summed offensive production this source projects (yards + 10x TDs + receptions). A crude
    #: scale-free proxy on purpose: the contamination test only asks whether the source projects
    #: ANY production for a man who will play zero games, which is a failed identity rather than
    #: a magnitude. Scoring it under league rules would add a number the test does not use.
    production: float | None
    detail: str
    publishes_games: bool
    behind: bool
    verdict: str
    #: dv the board loses if this source's whole statline is rejected. Only computed for the
    #: sources actually judged stale, because it costs a board revaluation each.
    reject_impact: float | None = None


@dataclass
class Row:
    """One player's full cross-source picture plus the proposed action."""

    finding: Finding
    pos: str
    team: str
    adp: float | None
    credited_games: float | None
    curve_games: float | None
    positional_rank: int | None
    sources: list[SourceView] = field(default_factory=list)
    proposed_games: float | None = None
    clamped_to_curve: bool = False
    proposed_rejections: list[str] = field(default_factory=list)
    board_impact: float | None = None
    action: str = ""

    @property
    def behind_sources(self) -> list[str]:
        return [s.source for s in self.sources if s.behind]


# --------------------------------------------------------------------------- loading


def _fail(index: int, entry: object, problem: str) -> None:
    raise InjuryResearchError(
        f"data/injury_research.json entry {index} ({entry!r}): {problem}. "
        "Fix the file rather than deleting the entry -- a dropped line is a decision nobody made."
    )


def parse_research(payload: object) -> tuple[Finding, ...]:
    """Validate the research payload. Strict: this feeds a rejection and an override."""
    if isinstance(payload, Mapping):
        entries = payload.get("findings")
        if entries is None:
            raise InjuryResearchError(
                "data/injury_research.json has no 'findings' key. A bare list is accepted too, "
                "but a mapping without 'findings' is more likely a truncated write than an "
                "empty sweep."
            )
    else:
        entries = payload
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise InjuryResearchError("'findings' must be a list.")
    if not entries:
        raise InjuryResearchError(
            "data/injury_research.json exists but holds no findings. An EMPTY file is what a "
            "truncated write looks like; delete the file entirely to mean 'nothing researched'."
        )

    out: list[Finding] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            _fail(i, entry, "is not an object")
        pid = entry.get("player_id")
        # `player_id` is REQUIRED and may never be null. decisions.py gives null a real meaning
        # (the decision applies source-wide); an injury is a fact about ONE player and has no
        # such grain, so the same shape here is refused rather than reinterpreted.
        if not isinstance(pid, str) or not pid.strip():
            _fail(i, entry, "'player_id' must be a non-empty string (never null)")
        games_missed = entry.get("games_missed", 0)
        if isinstance(games_missed, bool) or not isinstance(games_missed, (int, float)):
            _fail(i, entry, "'games_missed' must be a number")
        if games_missed < 0:
            _fail(i, entry, "'games_missed' cannot be negative")
        season_ending = entry.get("season_ending", False)
        if not isinstance(season_ending, bool):
            _fail(i, entry, "'season_ending' must be true or false, not a string")
        for required in ("report_date", "citation"):
            if not str(entry.get(required, "")).strip():
                _fail(
                    i,
                    entry,
                    f"'{required}' is required -- an injury claim with no source and no date is "
                    "exactly the unverifiable input this file exists to prevent",
                )
        out.append(
            Finding(
                player_id=str(pid).strip(),
                player_name=str(entry.get("player_name", "")).strip(),
                status=str(entry.get("status", "")).strip(),
                season_ending=season_ending,
                games_missed=float(games_missed),
                confidence=str(entry.get("confidence", "")).strip().upper(),
                report_date=str(entry.get("report_date", "")).strip(),
                citation=str(entry.get("citation", "")).strip(),
                notes=str(entry.get("notes", "")).strip(),
            )
        )
    return tuple(out)


def load_research(path: Path | None = None) -> tuple[Finding, ...]:
    """Missing file -> no findings. Present but broken -> raise. Same rule as the sibling files."""
    path = path or RESEARCH_PATH
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InjuryResearchError(f"{path} is not valid JSON: {exc}") from exc
    return parse_research(payload)


# --------------------------------------------------------------------------- the sweep


#: The component stats that mean "this source thinks he will produce". Deliberately excludes
#: `games` (a games figure with an all-zero statline is not production) and the attempt/target
#: counts (volume without output). Touchdowns are weighted x10 only so a TD-only projection is
#: not rounded away next to yardage; nothing downstream reads this number as points.
_PRODUCTION_WEIGHTS: Mapping[str, float] = {
    "pass_yd": 1.0,
    "rush_yd": 1.0,
    "rec_yd": 1.0,
    "rec": 1.0,
    "pass_td": 10.0,
    "rush_td": 10.0,
    "rec_td": 10.0,
}


def _production(line: object) -> float:
    """Summed projected production. Zero means the source is already treating him as done."""
    total = 0.0
    for stat, weight in _PRODUCTION_WEIGHTS.items():
        value = getattr(line, stat, None)
        if isinstance(value, (int, float)):
            total += float(value) * weight
    return total


def _detail(line: object) -> str:
    """The few stats a human needs to see to judge whether a source has caught up."""
    parts = []
    for stat in ("games", "pass_yd", "rush_yd", "rec", "rec_yd"):
        value = getattr(line, stat, None)
        if isinstance(value, (int, float)) and value:
            parts.append(f"{stat}={value:g}")
    return ", ".join(parts) or "all zero"


@dataclass(frozen=True)
class Decision:
    """What to do about one player. ``games is None`` means write nothing."""

    games: float | None
    action: str
    clamped_to_curve: bool = False


def decide_action(
    finding: Finding,
    *,
    credited: float | None,
    curve: float | None,
    weeks: float,
    behind: Sequence[str] = (),
) -> Decision:
    """The whole rule, as a pure function so it can be pinned by tests without a board.

    Extracted deliberately: the asymmetry below is the single most load-bearing line in this
    tool and the first version of it was WRONG (it proposed raising a player's games because a
    press report was rosier than the source). Logic that has already been wrong once belongs
    somewhere a test can reach it.
    """
    if finding.is_severe:
        # Season over. Zero games, which zeroes value while leaving him on the board so a pick
        # can still be RECORDED against him -- bookkeeping is this tool's first job.
        return Decision(games=0.0, action="override games -> 0.0")

    if finding.games_missed <= 0:
        return Decision(games=None, action="no change -- research says he plays a full season")

    target = max(0.0, weeks - finding.games_missed)
    clamped = curve is not None and target > curve
    effective = min(target, curve) if curve is not None else target

    if credited is not None and effective >= credited - 1e-9:
        # THE ASYMMETRY, and the reason this is not a mechanical rewrite of the games figure.
        # The research number is authoritative only DOWNWARD. A source already more pessimistic
        # than the reporting is not "behind" -- it is being more careful than the beat writer,
        # and raising the figure to match a press report is precisely the error direction that
        # inflates a player Marc then drafts at full value. Same shape as playing_time's curve
        # clamp, one level up: bad news passes through, good news does not.
        return Decision(
            games=None,
            clamped_to_curve=clamped,
            action=(
                f"NO CHANGE -- the board already credits {credited:.2f} games, at or below the "
                f"{effective:.2f} this reporting implies. The source is more conservative than "
                "the news; an upward override is never applied."
            ),
        )

    action = f"override games -> {effective:.2f}"
    if behind:
        action += f" (behind: {', '.join(behind)})"
    return Decision(games=effective, action=action, clamped_to_curve=clamped)


def sweep(inputs: ReviewInputs, findings: Sequence[Finding]) -> list[Row]:
    """Build the cross-source picture and the proposed action for every researched player."""
    engine = ImpactEngine(inputs)
    effective = effective_games_by_pid(inputs)
    weeks = float(inputs.cfg.weeks)
    rows: list[Row] = []

    for finding in findings:
        pid = finding.player_id
        credited, curve, rank = effective.get(pid, (None, None, None))
        row = Row(
            finding=finding,
            pos=inputs.pos_of.get(pid, "?"),
            team=inputs.team_of.get(pid, "?"),
            adp=inputs.adp_of.get(pid),
            credited_games=credited,
            curve_games=curve,
            positional_rank=rank,
        )

        # --- the arithmetic that decides the lever, in both branches -------------------
        if finding.is_severe:
            target = 0.0
        else:
            target = max(0.0, weeks - finding.games_missed)

        for source in inputs.sources:
            line = inputs.statlines_by_source.get(source, {}).get(pid)
            pub_games = getattr(line, "games", None) if line is not None else None
            production = _production(line) if line is not None else None
            publishes = source in inputs.games_sources

            behind = False
            if line is None:
                verdict = "no row at all -- this source never published him"
            elif finding.is_severe and (production or 0.0) > 0.0:
                # A failed identity, not a distance: he plays zero games, this source projects
                # a season. Nothing about the other sources' numbers is consulted.
                behind = True
                verdict = "STALE: still projects a full statline for a player who is done"
            elif finding.is_severe:
                verdict = "already zeroed -- this source has caught up"
            elif publishes and pub_games is not None and pub_games > target + 1e-9:
                behind = True
                verdict = f"BEHIND on availability: publishes {pub_games:g} games vs {target:g}"
            elif publishes and pub_games is not None:
                verdict = f"consistent ({pub_games:g} games)"
            else:
                # Cannot be behind on a figure it does not publish. Saying otherwise would
                # invent a games number for a source that deliberately has none.
                verdict = "publishes no games column -- cannot be behind or ahead"

            view = SourceView(
                source=source,
                games=pub_games,
                production=production,
                detail=_detail(line) if line is not None else "no row",
                publishes_games=publishes,
                behind=behind,
                verdict=verdict,
            )
            if behind and finding.is_severe:
                # Only the stale-statline case is expressible as a rejection, so only it gets
                # the board-impact column. An availability override moves games, not a source,
                # and the ImpactEngine has nothing to say about it -- exactly the point the
                # review queue's injury rows already make.
                try:
                    impact = engine.for_player(pid, source, "*")
                    view.reject_impact = getattr(impact, "dv_delta", None)
                except Exception:  # noqa: BLE001 - diagnostic only, never load-bearing
                    view.reject_impact = None
            row.sources.append(view)

        # --- proposed action ------------------------------------------------------------
        decision = decide_action(
            finding, credited=credited, curve=curve, weeks=weeks, behind=row.behind_sources
        )
        row.proposed_games = decision.games
        row.clamped_to_curve = decision.clamped_to_curve
        row.action = decision.action
        if finding.is_severe:
            row.proposed_rejections = [s.source for s in row.sources if s.behind]
            if row.proposed_rejections:
                row.action += f"; reject statline from {', '.join(row.proposed_rejections)}"

        # The player-level number worth reporting is how far the games figure moves, because
        # that is what an availability override actually changes. The dv consequence shows up
        # on the rebuilt board after --apply, which is the only place it is real.
        if row.credited_games is not None and row.proposed_games is not None:
            after_clamp = (
                min(row.proposed_games, row.curve_games)
                if row.curve_games is not None
                else row.proposed_games
            )
            row.board_impact = after_clamp - row.credited_games

        rows.append(row)

    rows.sort(key=lambda r: (r.adp if r.adp is not None else 9_999))
    return rows


# --------------------------------------------------------------------------- output


def render(rows: Sequence[Row], weeks: float) -> str:
    """The human report. Every number shown is one a source published or research implied."""
    if not rows:
        return (
            "No injury research on file (data/injury_research.json absent).\n"
            "Nothing swept, nothing changed."
        )
    out: list[str] = []
    out.append("=" * 96)
    out.append("INJURY SWEEP -- external reporting vs what each source still publishes")
    out.append("=" * 96)
    severe = [r for r in rows if r.finding.is_severe]
    behind_any = [r for r in rows if r.behind_sources]
    out.append(
        f"{len(rows)} researched player(s) | {len(severe)} season-ending | "
        f"{len(behind_any)} with at least one source behind | league weeks = {weeks:.0f}"
    )
    out.append("")
    out.append(
        "Availability has ONE source (ESPN); Sleeper's games is a blanket constant and "
        "FantasyPros/FantasySharks publish none. So 'behind on availability' can only ever "
        "name ESPN -- that is a fact about the feeds, not a bug in this report."
    )
    out.append("")

    for row in rows:
        f = row.finding
        flag = "  *** SEASON-ENDING ***" if f.is_severe else ""
        out.append("-" * 96)
        out.append(
            f"{f.player_name or f.player_id} ({row.pos} {row.team})  ADP "
            f"{row.adp if row.adp is not None else 'unranked'}{flag}"
        )
        out.append(
            f"  research: {f.status or 'status unknown'} | misses {f.games_missed:g} of "
            f"{weeks:.0f} | confidence {f.confidence or 'UNSTATED'} | "
            f"reported {f.report_date}"
        )
        if f.notes:
            out.append(f"  note: {f.notes}")
        out.append(f"  cite: {f.citation}")
        credited = "n/a" if row.credited_games is None else f"{row.credited_games:.2f}"
        curve = "n/a" if row.curve_games is None else f"{row.curve_games:.2f}"
        rank = "n/a" if row.positional_rank is None else str(row.positional_rank)
        out.append(
            f"  board currently credits {credited} games "
            f"(healthy-rank curve for {row.pos}{rank} = {curve})"
        )
        for s in row.sources:
            mark = "!!" if s.behind else "  "
            extra = "" if s.reject_impact is None else f"  [rejecting it moves {s.reject_impact:+.1f} dv]"
            out.append(f"   {mark} {s.source:14} {s.detail:34} {s.verdict}{extra}")
        impact = (
            "n/a" if row.board_impact is None else f"{row.board_impact:+.2f} games"
        )
        out.append(f"  ACTION: {row.action}")
        if row.clamped_to_curve:
            out.append(
                "          (the arithmetic target sits above the curve, so the curve clamps it "
                "-- upward claims stop at typical availability for the rank)"
            )
        out.append(f"  games change if applied: {impact}")
    out.append("-" * 96)
    return "\n".join(out)


def apply(
    rows: Sequence[Row],
    *,
    today: str,
    only_severe: bool = False,
    overrides_path: Path | None = None,
    decisions_path: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Write the overrides and the contamination rejections. Both via validated constructors.

    ``only_severe`` commits ONLY the season-ending rows. That is the useful split in the days
    before a draft: a season-ending injury is settled fact and can be applied the moment it is
    reported, while a PUP or a soft-tissue timeline is still moving and resolves at the 53-man
    cutdown. The rows not applied stay in the research file with their citations intact -- they
    are deferred, not dropped, and the next run picks them up.
    """
    overrides = []
    rejections = []
    for row in rows:
        f = row.finding
        if row.proposed_games is None:
            continue
        if only_severe and not f.is_severe:
            continue
        if not f.is_severe and f.games_missed <= 0:
            continue  # research says he is fine; writing an inert override would be noise
        overrides.append(
            pt.new_override(
                player_id=f.player_id,
                games=float(row.proposed_games),
                reason=(
                    f"{f.status or 'injury'}: external reporting {f.report_date} has him missing "
                    f"{f.games_missed:g} games ({f.confidence or 'confidence unstated'}). {f.citation}"
                ).strip(),
                player_name=f.player_name,
                designation=f.status,
                date=today,
            )
        )
        for source in row.proposed_rejections:
            rejections.append(
                dc.new_decision(
                    source=source,
                    stat="*",
                    player_id=f.player_id,
                    verdict="reject",
                    reason=(
                        f"CONTAMINATION: {source} still projects a full season for a player "
                        f"whose season is over per reporting {f.report_date}. {f.citation}"
                    ),
                    player_name=f.player_name,
                    detector=DETECTOR,
                    date=today,
                )
            )

    pt_path = dc_path = None
    if overrides:
        merged = pt.merge_overrides(pt.load_overrides(overrides_path), overrides)
        pt_path = pt.save_overrides(merged, overrides_path)
    if rejections:
        merged_d = dc.merge_decisions(dc.load_decisions(decisions_path), rejections)
        dc_path = dc.save_decisions(merged_d, decisions_path)
    return pt_path, dc_path


def to_json(rows: Sequence[Row], weeks: float) -> dict[str, Any]:
    return {
        "schema": 1,
        "league_weeks": weeks,
        "rows": [
            {
                "player_id": r.finding.player_id,
                "player_name": r.finding.player_name,
                "pos": r.pos,
                "team": r.team,
                "adp": r.adp,
                "status": r.finding.status,
                "season_ending": r.finding.season_ending,
                "games_missed": r.finding.games_missed,
                "confidence": r.finding.confidence,
                "report_date": r.finding.report_date,
                "citation": r.finding.citation,
                "credited_games": r.credited_games,
                "curve_games": r.curve_games,
                "positional_rank": r.positional_rank,
                "proposed_games": r.proposed_games,
                "clamped_to_curve": r.clamped_to_curve,
                "behind_sources": r.behind_sources,
                "proposed_rejections": r.proposed_rejections,
                "action": r.action,
                "board_impact": r.board_impact,
                "sources": [
                    {
                        "source": s.source,
                        "games": s.games,
                        "production": s.production,
                        "detail": s.detail,
                        "reject_impact": s.reject_impact,
                        "publishes_games": s.publishes_games,
                        "behind": s.behind,
                        "verdict": s.verdict,
                    }
                    for s in r.sources
                ],
            }
            for r in rows
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ or "")
    ap.add_argument("--research", type=Path, default=None, help="path to injury_research.json")
    ap.add_argument("--apply", action="store_true", help="write the overrides and rejections")
    ap.add_argument(
        "--only-severe",
        action="store_true",
        help=(
            "with --apply, commit ONLY the season-ending rows. The still-moving ones stay in "
            "the research file, deferred rather than dropped, for the next run."
        ),
    )
    ap.add_argument("--json", type=Path, default=None, help="also dump the sweep as JSON")
    ap.add_argument("--season", type=int, default=2026)
    args = ap.parse_args(argv)

    findings = load_research(args.research)
    inputs = load_review_inputs(season=args.season)
    rows = sweep(inputs, findings)
    weeks = float(inputs.cfg.weeks)

    print(render(rows, weeks))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_json(rows, weeks), indent=1), encoding="utf-8")
        print(f"\nsweep JSON: {args.json}")

    if args.apply:
        pt_path, dc_path = apply(
            rows, today=date.today().isoformat(), only_severe=args.only_severe
        )
        if args.only_severe:
            deferred = [r for r in rows if not r.finding.is_severe and r.proposed_games is not None]
            if deferred:
                print(
                    '--only-severe: DEFERRED (still on file, not applied): '
                    + ', '.join(f'{r.finding.player_name} -> {r.proposed_games:g} games' for r in deferred)
                )
        print()
        print(f"playing-time overrides written: {pt_path or 'none'}")
        print(f"projection rejections written : {dc_path or 'none'}")
        print(
            "Re-run tools/run_invariants.py and rebuild the board -- an applied decision must be "
            "visible on the board (CLAUDE.md)."
        )
    else:
        print("\n(report only -- nothing written. Re-run with --apply to commit these.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
