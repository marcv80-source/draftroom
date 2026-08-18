"""Clay PDF ingest adapter: Mike Clay's ESPN draft-kit projections PDF.

WHY THIS SOURCE MATTERS MORE THAN THE OTHERS: Sleeper carries no target
field under any name (verified across all 3,111 of its projection
records) and the FantasyPros manual CSV export (prep/manual_csv.py) has no
targets column either -- confirmed against the real WR header
(`REC,YDS,TDS,ATT,YDS,TDS,FL,FPTS`). Targets are this project's single
biggest blind spot for receivers. Clay's PDF carries a real `Targ` column
for every RB/WR/TE row. That is the whole point of this adapter.

WHERE THE FILE COMES FROM: `data/manual/clay_projections_<download
date>.pdf`, an ESPN draft kit PDF Marc downloads and drops into
`data/manual/` by hand -- same "manual drop, no API" pattern as
`manual_csv.py`, just a PDF instead of a CSV. This module makes ZERO
network calls.

PAGE LAYOUT (confirmed 2026-08-17 against the real 82-page PDF): rather
than trust the glossary page's claimed page *numbers* (which shift if
ESPN adds/removes a page in a future refresh), this module finds sections
by their own page TITLE text ("Quarterback Projections", "Running Back
Projections", "Wide Receiver Projections", "Tight End Projections", each
possibly followed by " (n/m)" when a position spans multiple pages) and
skips every other page (team pages, IDP positions, Kicker, Returner,
Category Leaderboard, glossary) by simply not recognizing their titles.
IDP is skipped even though it lives in the same numeric page range as the
four positions above, per the task spec.

COLUMN LAYOUT -- CONFIRMED DIFFERENT FROM THE task's OWN OBSERVED-LAYOUT
NOTE: the task description's sketch of "PASSING: Att Comp Yds TD INT Sk |
RUSHING: Att Yds TD | RECEIVING: Tgt Rec Yd TD | PPR: Pts Rk" describes the
per-TEAM pages (pages 2-33), not the positional pages this adapter reads.
The real positional-page headers, extracted via pdfplumber and verified
against actual data rows by x-coordinate (see docs below), are:

    QB: Player Team PosRk FFPt G  P-Att Comp P-Yds P-TD INT Sk  Carry Ru-Yds Ru-TD
        (no receiving block at all for QB)
    RB/WR/TE (identical layout across all three positions):
        Player Team PosRk FFPt G  Carry Ru-Yds Ru-TD  Targ Rec Re-Yd Re-TD  Car% Targ%

Every RB/WR/TE row carries BOTH a rushing block and a receiving block
(receivers get occasional jet-sweep/end-around carries; backs get target
share) -- this is not a "primary stat first" layout, it is one fixed
column order for all three positions, confirmed against real WR/TE rows
(e.g. Trey McBride, a receiving-only tight end, shows explicit 0/0/0 in
the rushing columns rather than omitting them).

THE DUPLICATE-HEADER TRAP, AND HOW THIS MODULE AVOIDS IT: like
FantasyPros' CSV export (see manual_csv.py's module docstring), Clay's
column headers repeat text across blocks -- "Yds" and "TD" appear twice
in the QB row (once for passing, once for rushing), and "Yds"/"TD" appear
twice again in the RB/WR/TE row (rushing, then receiving). Header TEXT is
therefore useless for disambiguation on its own. This module never infers
column meaning from header text at parse time. Instead:
  1. The header line is still checked against a hardcoded, exact-string
     expected header per position -- not to derive the mapping, but to
     fail loudly (ClayColumnDriftError) if ESPN's real columns ever drift
     from what was verified here.
  2. Every DATA row is tokenized by whitespace-split and resolved
     POSITIONALLY: a fixed count of trailing tokens (13, for every
     position) maps 1:1 onto the known column layout in order; every
     token before that is the player's name (handles multi-word names,
     "Jr."/"III" suffixes, and hyphenated/apostrophe'd names -- confirmed
     against "Ken Walker III", "Brian Thomas Jr.", "Amon-Ra St. Brown"-style
     rows -- since pdfplumber's extract_text() only splits on whitespace,
     a hyphenated or apostrophe'd name is already one token). This is
     robust regardless of exact pixel column boundaries, because every
     stat VALUE in this table is a single whitespace-free token (no
     "1,373"-with-commas, no embedded spaces) -- confirmed against the
     real extracted rows.
  3. THE COLUMN-ALIGNMENT PROOF the task asks for is exactly this: pick
     any RB/WR/TE row and read off token N (0-indexed from the end) --
     rush_yd is always the 2nd-from-last-but-eleven token (index -12 of
     the 13 trailing tokens) and rec_yd is always index -8, regardless of
     the player's name length. Trey McBride's row
     "Trey McBride ARZ 1 242 17 0 0 0 149 108 1023 5 0% 26%" reads
     rush_att=0, rush_yd=0, rush_td=0 (a real receiving-only TE has zero
     rush volume, exactly as expected) then rec_tgt=149, rec=108,
     rec_yd=1023, rec_td=5 -- rushing and receiving landed in the fields
     their real football profile predicts, not swapped.

WHAT IS DISCARDED, AND WHY (never mapped to a canonical stat):
  - `FF Pt` (Clay's own half-PPR-ish point total) and `Pos Rk` (rank
    within position): every adapter in this pipeline emits component
    stats, never fantasy points (CLAUDE.md "Canonical stat vocabulary";
    see manual_csv.py's `_FPTS_DISCARD_NOTE` for the FantasyPros
    equivalent). Ingesting Clay's own point total would let a foreign
    scoring system leak into the model and defeats the entire point of
    the scoring-reconciliation gate: proving OUR re-scoring matches
    Yahoo, not matching Clay.
  - `Sk` (sacks taken, QB only): no canonical slot exists for sacks in
    CANONICAL_STATS, and this league's Yahoo `stat_modifiers` carry no
    scoring impact for sacks taken. Dropped explicitly, not silently --
    every row's Sk token is read (to keep positional alignment correct
    for the columns after it) and then discarded.
  - `Car%` / `Targ%` (share-of-team metrics, RB/WR/TE only): these are
    derived shares, not raw per-player counting stats, and have no
    canonical slot either. Dropped the same way.

GAMES: carried straight from the `G` column (every real row shows an
explicit integer, no blanks observed in the 2026-08-17 file).

THE INJURY-DISCOUNT NOTE (Clay's own words, from the PDF's glossary page):
these are 17-game projections, and Clay advises considering "removing ~2
games worth of stats for QBs, WRs and TE, and ~3 games for RBs" as an
availability baseline. This module does NOT apply that discount silently
-- see `apply_injury_discount` on `extract_projections()`. It is exposed
as an optional, clearly named, default-OFF parameter. Whether to use it
is Marc's call, not this adapter's.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from draftroom.prep.schema import CANONICAL_STATS, StatLine

log = logging.getLogger("draftroom.prep.clay_pdf")

SOURCE = "clay_pdf"

# backend/draftroom/prep/clay_pdf.py -> parents[3] == repo root (C:\dev\draftroom),
# same depth convention as manual_csv.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parents[3]
MANUAL_DIR = REPO_ROOT / "data" / "manual"

# Primary filename form Marc actually uses (confirmed 2026-08-17):
# `clay_projections_<YYYY-MM-DD>.pdf`, the download/update date ESPN
# stamps on the draft kit (NOT necessarily the season -- 2026-08-17 is a
# date, and the season it covers has to be supplied by the caller, same
# division of responsibility as manual_csv.py's dated form).
FILENAME_RE = re.compile(r"^clay_projections_(?P<date>\d{4}-\d{2}-\d{2})\.pdf$", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Section detection: match by page TITLE text, not by page number. The
# task's glossary page claims pages 34-57 hold QB/RB/WR/TE/IDP, but real
# page numbers shift across PDF refreshes -- title text does not. Titles
# may carry a " (n/m)" suffix when a position spans multiple pages (e.g.
# "Wide Receiver Projections (3/5)"); that suffix is stripped before
# lookup. Every OTHER title (team pages, IDP positions, Kicker, Returner,
# Category Leaderboard, the glossary/leaderboard divider page) simply
# doesn't match and its page is skipped -- this is how IDP gets excluded
# without needing to track "when do we stop".
# ---------------------------------------------------------------------------
_PAGINATION_SUFFIX_RE = re.compile(r"\s*\(\d+/\d+\)\s*$")

TITLE_TO_POS: dict[str, str] = {
    "Quarterback Projections": "qb",
    "Running Back Projections": "rb",
    "Wide Receiver Projections": "wr",
    "Tight End Projections": "te",
}

POSITIONS: tuple[str, ...] = ("qb", "rb", "wr", "te")

# Exact header line per position, verified 2026-08-17 against the real PDF
# (see module docstring). Checked at parse time to fail loudly on real
# column drift -- never used to derive the mapping (see ClayColumnDriftError).
EXPECTED_HEADERS: dict[str, str] = {
    "qb": "Quarterback Team Pos Rk FF Pt G P Att Comp P Yds P TD INT Sk Carry Ru Yds Ru TD",
    "rb": "Running Back Team Pos Rk FF Pt G Carry Ru Yds Ru TD Targ Rec Re Yd Re TD Car% Targ%",
    "wr": "Wide Receiver Team Pos Rk FF Pt G Carry Ru Yds Ru TD Targ Rec Re Yd Re TD Car% Targ%",
    "te": "Tight End Team Pos Rk FF Pt G Carry Ru Yds Ru TD Targ Rec Re Yd Re TD Car% Targ%",
}

# Positional field names for the fixed trailing block of tokens on every
# data row (everything after the player's name). `None` in
# CANONICAL_FIELD_MAP below means "read it -- to keep alignment correct
# for what follows -- then discard it" (see module docstring for why each
# one is dropped).
_RB_WR_TE_LAYOUT: tuple[str, ...] = (
    "team", "pos_rk", "ff_pt", "games",
    "rush_att", "rush_yd", "rush_td",
    "rec_tgt", "rec", "rec_yd", "rec_td",
    "car_pct", "tgt_pct",
)

FIELD_LAYOUTS: dict[str, tuple[str, ...]] = {
    "qb": (
        "team", "pos_rk", "ff_pt", "games",
        "pass_att", "pass_cmp", "pass_yd", "pass_td", "pass_int", "sk",
        "rush_att", "rush_yd", "rush_td",
    ),
    "rb": _RB_WR_TE_LAYOUT,
    "wr": _RB_WR_TE_LAYOUT,
    "te": _RB_WR_TE_LAYOUT,
}

# field name -> canonical StatLine attribute, or None to discard.
CANONICAL_FIELD_MAP: dict[str, str | None] = {
    "team": None,  # not a stat -- consumed for the "name|team" key
    "pos_rk": None,  # Pos Rk: rank within position -- fantasy-points-derived, see module docstring
    "ff_pt": None,  # FF Pt: Clay's own point total -- never ingested, see module docstring
    "games": "games",
    "pass_att": "pass_att",
    "pass_cmp": "pass_cmp",
    "pass_yd": "pass_yd",
    "pass_td": "pass_td",
    "pass_int": "pass_int",
    "sk": None,  # Sk: sacks taken -- no canonical slot, no scoring impact in this league
    "rush_att": "rush_att",
    "rush_yd": "rush_yd",
    "rush_td": "rush_td",
    "rec_tgt": "rec_tgt",
    "rec": "rec",
    "rec_yd": "rec_yd",
    "rec_td": "rec_td",
    "car_pct": None,  # Car%: derived share, not a raw stat, no canonical slot
    "tgt_pct": None,  # Targ%: derived share, not a raw stat, no canonical slot
}

# Clay's own words (glossary page of the PDF): "consider removing ~2 games
# worth of stats for QBs, WRs and TE, and ~3 games for RBs" as a
# 17-game -> availability-adjusted baseline. See apply_injury_discount().
INJURY_DISCOUNT_GAMES: dict[str, float] = {"qb": 2.0, "rb": 3.0, "wr": 2.0, "te": 2.0}


class ClayPdfError(RuntimeError):
    """Base error for the Clay PDF adapter. Every failure mode here is loud
    on purpose -- see CLAUDE.md: an unmapped column or a malformed row must
    never be silently accepted or silently dropped without being counted."""


class ClayColumnDriftError(ClayPdfError):
    """A position's header line doesn't match the exact text verified
    2026-08-17 (EXPECTED_HEADERS). Real column drift, never guessed at --
    update EXPECTED_HEADERS/FIELD_LAYOUTS deliberately, with the real
    header pasted into the commit, if ESPN's export genuinely changes."""


class TooFewPlayersError(ClayPdfError):
    """Total parsed QB+RB+WR+TE rows fell below the plausible floor.

    Catches a truncated/corrupted PDF read (e.g. pdfplumber only got a few
    pages, or a mid-file extraction failure) that would otherwise return a
    thin, silently-degraded player pool.
    """


# Set well under the real observed total (40 QB + 111 RB + 187 WR + 80 TE
# = 418 rows, confirmed 2026-08-17) so this catches a genuinely truncated
# read without false-alarming on a normal file.
MIN_TOTAL_PLAYERS = 350


@dataclass
class RowSkip:
    """One row this module refused to parse, kept instead of silently dropped."""

    position: str
    page_label: str
    reason: str
    raw_line: str


@dataclass
class ClayExtractionReport:
    """Diagnostics from one extract_projections() run -- counts by
    position, how many rows were skipped and why, and whether the
    injury discount was applied. Never silently thrown away; every
    caller that wants the plain dict can call extract_projections()
    and ignore this, but the counts are always logged too."""

    counts_by_position: dict[str, int] = field(default_factory=dict)
    skips: list[RowSkip] = field(default_factory=list)
    apply_injury_discount: bool = False

    @property
    def total_players(self) -> int:
        return sum(self.counts_by_position.values())

    @property
    def skipped_count(self) -> int:
        return len(self.skips)


def _strip_pagination_suffix(title: str) -> str:
    return _PAGINATION_SUFFIX_RE.sub("", title.strip())


def _identify_position(lines: list[str], page_label: str) -> str | None:
    """Given one page's extracted text lines, return which of qb/rb/wr/te
    this page is (by TITLE text, ignoring an optional trailing " (n/m)"),
    or None if this page isn't one of the four we care about (team pages,
    IDP, Kicker, Returner, Category Leaderboard, the glossary/divider page
    all return None and are simply skipped by the caller).

    Pure function, no PDF library involved -- exercised directly by
    tests/test_clay_pdf.py, including the column-drift failure path, without
    needing a real or fixture PDF file for that specific check.

    Raises ClayColumnDriftError if the title matches a known position but
    the header line (lines[1]) doesn't match the exact text verified
    against the real PDF (EXPECTED_HEADERS) -- real column drift, never
    guessed at.
    """
    if not lines:
        return None
    title = _strip_pagination_suffix(lines[0])
    pos = TITLE_TO_POS.get(title)
    if pos is None:
        return None

    if len(lines) < 2 or lines[1].strip() != EXPECTED_HEADERS[pos]:
        seen_header = lines[1].strip() if len(lines) >= 2 else "<no second line>"
        raise ClayColumnDriftError(
            f"{page_label}: header line doesn't match the known {pos.upper()} layout.\n"
            f"  expected: {EXPECTED_HEADERS[pos]!r}\n"
            f"  saw:      {seen_header!r}\n"
            "This is a hard failure (never guessed at) -- if ESPN's export genuinely "
            "changed columns, update EXPECTED_HEADERS/FIELD_LAYOUTS in clay_pdf.py "
            "deliberately, with the real header pasted into the commit."
        )
    return pos


def _parse_section_lines(
    lines: list[str], pos: str, page_label: str, report: ClayExtractionReport,
) -> dict[str, StatLine]:
    """Parse the data rows of ONE already-identified position page (lines
    AFTER the title and header lines have been stripped by the caller).

    Pure function, no PDF library involved -- this is what
    tests/test_clay_pdf.py exercises directly with hardcoded text fixtures,
    so the column-resolution logic is tested without ever opening a PDF.
    """
    layout = FIELD_LAYOUTS[pos]
    n = len(layout)
    out: dict[str, StatLine] = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue  # blank line between/after tables

        tokens = line.split()
        if len(tokens) < n + 1:
            report.skips.append(RowSkip(
                position=pos, page_label=page_label,
                reason=f"only {len(tokens)} tokens, need at least {n + 1} (name + {n} stat columns)",
                raw_line=raw,
            ))
            log.warning(
                "clay_pdf: %s (%s) skipping row with only %d tokens (need >= %d): %r",
                page_label, pos.upper(), len(tokens), n + 1, raw,
            )
            continue

        # Positional resolution, never header-text resolution (see module
        # docstring): the LAST n tokens are always the fixed stat block in
        # FIELD_LAYOUTS order; everything before that is the player's name,
        # however many tokens it takes (multi-word names, "Jr."/"III"
        # suffixes, hyphenated/apostrophe'd single-token names all just work).
        name = " ".join(tokens[: len(tokens) - n])
        stat_tokens = tokens[len(tokens) - n :]
        row = dict(zip(layout, stat_tokens))
        team = row["team"]

        kwargs: dict[str, float] = {}
        bad_field = None
        bad_value = None
        try:
            for field_name, value in row.items():
                canonical = CANONICAL_FIELD_MAP[field_name]
                if canonical is None:
                    continue  # Pos Rk / FF Pt / Sk / Car% / Targ% -- see module docstring
                bad_field, bad_value = field_name, value
                kwargs[canonical] = float(value)
        except ValueError:
            report.skips.append(RowSkip(
                position=pos, page_label=page_label,
                reason=f"non-numeric value {bad_value!r} in column {bad_field!r}",
                raw_line=raw,
            ))
            log.warning(
                "clay_pdf: %s (%s) skipping row with non-numeric %s=%r: %r",
                page_label, pos.upper(), bad_field, bad_value, raw,
            )
            continue

        if not name:
            # Shouldn't happen (tokens[:0] only if len(tokens) == n exactly,
            # already excluded by the length check above), but never accept
            # a blank name as a "player" -- see manual_csv.py's equivalent guard.
            report.skips.append(RowSkip(
                position=pos, page_label=page_label, reason="blank player name", raw_line=raw,
            ))
            continue

        key = f"{name}|{team}"
        if key in out:
            log.warning(
                "clay_pdf: %s (%s) duplicate key %r -- overwriting with the later row",
                page_label, pos.upper(), key,
            )
        out[key] = StatLine(**kwargs)

    return out


def apply_injury_discount_to_stat(stat: StatLine, pos: str) -> StatLine:
    """Apply Clay's own suggested availability haircut to one player's
    17-game projection: "consider removing ~2 games worth of stats for
    QBs, WRs and TE, and ~3 games for RBs."

    Implemented as a simple rate-preserving reduction: every counting stat
    is scaled by (games - discount_games) / games, and `games` itself is
    reduced by the same amount. This is a JUDGMENT CALL on how to operationalize
    Clay's qualitative advice into a number -- flagged for review, not this
    adapter's decision to make silently. OFF by default everywhere in this
    module; a caller must opt in explicitly via extract_projections(..., apply_injury_discount=True).

    Named `..._to_stat` (not just `apply_injury_discount`) so it doesn't
    shadow the identically-worded `apply_injury_discount` boolean parameter
    on extract_projections()/extract_projections_with_report().
    """
    discount = INJURY_DISCOUNT_GAMES[pos]
    if stat.games <= 0:
        return stat
    new_games = max(0.0, stat.games - discount)
    factor = new_games / stat.games
    data = stat.as_dict()
    data["games"] = new_games
    for name in CANONICAL_STATS:
        if name == "games":
            continue
        data[name] = data[name] * factor
    return StatLine(**data)


def extract_projections(
    pdf_path: Path | str,
    *,
    apply_injury_discount: bool = False,
    min_total_players: int = MIN_TOTAL_PLAYERS,
) -> dict[str, StatLine]:
    """Extract QB/RB/WR/TE projections from Clay's ESPN draft-kit PDF.

    Returns a dict keyed "Player|Team" (matching manual_csv.py's
    convention -- Clay has no player IDs either), combined across all four
    positions. IDP pages are skipped entirely (see module docstring).

    apply_injury_discount: default False. When True, every player's stats
    are run through this module's `apply_injury_discount()` function using
    Clay's own suggested per-position game counts before being returned.
    Someone else owns whether this is the right call for the model; this
    parameter exists so the choice is explicit and visible, never baked in
    silently.

    min_total_players: floor for TooFewPlayersError (default
    MIN_TOTAL_PLAYERS). Exposed so a caller (e.g. a test running against a
    deliberately small fixture PDF) can override it rather than disabling
    the check entirely.

    Raises ClayColumnDriftError if a recognized position's header line
    doesn't match the exact text verified against the real PDF, and
    TooFewPlayersError if the total parsed player count falls implausibly
    low (a truncated/corrupted read). Individual malformed rows are never
    silently dropped -- see extract_projections_with_report() for the full
    per-row skip accounting.
    """
    statlines, _report = extract_projections_with_report(
        pdf_path, apply_injury_discount=apply_injury_discount, min_total_players=min_total_players,
    )
    return statlines


def extract_projections_with_report(
    pdf_path: Path | str,
    *,
    apply_injury_discount: bool = False,
    min_total_players: int = MIN_TOTAL_PLAYERS,
) -> tuple[dict[str, StatLine], ClayExtractionReport]:
    """Same as extract_projections(), but also returns the
    ClayExtractionReport (per-position counts, every skipped row with its
    reason, whether the discount was applied) -- this is what
    tools/ and the extraction summary should call directly rather than
    reconstructing counts by re-parsing."""
    import pdfplumber  # imported lazily so importing this module never requires the PDF lib at collection time

    path = Path(pdf_path)
    report = ClayExtractionReport(apply_injury_discount=apply_injury_discount)
    combined: dict[str, StatLine] = {}

    with pdfplumber.open(path) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            lines = text.splitlines()
            page_label = f"pdf page {page_idx + 1} ({lines[0].strip() if lines else '<blank>'})"
            pos = _identify_position(lines, page_label)
            if pos is None:
                continue  # team page, IDP, Kicker, Returner, Category Leaderboard, glossary, etc.

            page_rows = _parse_section_lines(lines[2:], pos, page_label, report)
            for key, stat in page_rows.items():
                if apply_injury_discount:
                    stat = apply_injury_discount_to_stat(stat, pos)
                if key in combined:
                    log.warning("clay_pdf: %r already present from a prior page -- overwriting", key)
                combined[key] = stat
                report.counts_by_position[pos] = report.counts_by_position.get(pos, 0) + 1

    if report.total_players < min_total_players:
        raise TooFewPlayersError(
            f"{path.name}: parsed only {report.total_players} QB+RB+WR+TE players "
            f"({report.counts_by_position}), below the floor of {min_total_players}. "
            "A real read of the full draft kit should be well over 400 -- this looks like "
            "a truncated or corrupted PDF read, not a real thin file."
        )

    log.info(
        "clay_pdf: extracted %d players from %s: %s (skipped %d rows)",
        report.total_players, path.name, report.counts_by_position, report.skipped_count,
    )
    return combined, report


# ---------------------------------------------------------------------------
# Caching: re-parsing a 5MB PDF on every prep run is wasteful, and a JSON
# cache is a reviewable diff (a re-parsed PDF is not). Cache is invalidated
# whenever the source PDF's mtime is newer than the cache file's, or when
# `force=True`.
# ---------------------------------------------------------------------------

def _find_source_pdf(manual_dir: Path = MANUAL_DIR) -> Path | None:
    """Newest `clay_projections_<date>.pdf` under manual_dir, by the date
    embedded in the filename (falls back to mtime if the filename doesn't
    match, so an oddly-renamed file doesn't just vanish silently)."""
    if not manual_dir.exists():
        return None
    candidates: list[tuple[str, Path]] = []
    for p in manual_dir.iterdir():
        if not p.is_file():
            continue
        m = FILENAME_RE.match(p.name)
        if m:
            candidates.append((m.group("date"), p))
    if candidates:
        candidates.sort(key=lambda t: t[0])
        return candidates[-1][1]
    # Fallback: any clay_projections_*.pdf, newest by mtime.
    loose = sorted(manual_dir.glob("clay_projections_*.pdf"), key=lambda p: p.stat().st_mtime)
    return loose[-1] if loose else None


def cache_path_for_season(season: int, manual_dir: Path = MANUAL_DIR) -> Path:
    return manual_dir / f"clay_parsed_{season}.json"


def _statline_to_json(stat: StatLine) -> dict[str, float]:
    return asdict(stat)


def _statline_from_json(data: dict[str, Any]) -> StatLine:
    return StatLine(**{k: float(v) for k, v in data.items() if k in CANONICAL_STATS})


def load_or_parse(
    season: int,
    *,
    pdf_path: Path | str | None = None,
    manual_dir: Path = MANUAL_DIR,
    apply_injury_discount: bool = False,
    force: bool = False,
    min_total_players: int = MIN_TOTAL_PLAYERS,
) -> tuple[dict[str, StatLine], ClayExtractionReport | None]:
    """Load `data/manual/clay_parsed_<season>.json` if it's fresh relative
    to the source PDF, otherwise re-parse the PDF and write the cache.

    Returns (statlines, report) -- report is None when served from cache
    (there is nothing to re-report; the cached file's own metadata carries
    the counts from when it was generated). Pass force=True to always
    re-parse regardless of cache freshness.
    """
    src = Path(pdf_path) if pdf_path is not None else _find_source_pdf(manual_dir)
    if src is None:
        raise ClayPdfError(
            f"no clay_projections_*.pdf found under {manual_dir}. "
            "Drop the ESPN draft kit PDF there (see task/CLAUDE.md for the filename convention)."
        )

    cache_file = cache_path_for_season(season, manual_dir)
    if not force and cache_file.exists():
        cache_mtime = cache_file.stat().st_mtime
        src_mtime = src.stat().st_mtime
        if cache_mtime >= src_mtime:
            with cache_file.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("apply_injury_discount", False) == apply_injury_discount:
                statlines = {k: _statline_from_json(v) for k, v in payload["players"].items()}
                log.info(
                    "clay_pdf: loaded %d players from cache %s (fresh vs. %s)",
                    len(statlines), cache_file.name, src.name,
                )
                return statlines, None
            log.info(
                "clay_pdf: cache %s exists but apply_injury_discount differs (cached=%s, "
                "requested=%s) -- re-parsing", cache_file.name,
                payload.get("apply_injury_discount"), apply_injury_discount,
            )

    statlines, report = extract_projections_with_report(
        src, apply_injury_discount=apply_injury_discount, min_total_players=min_total_players,
    )
    payload = {
        "source_pdf": src.name,
        "source_pdf_mtime": datetime.fromtimestamp(src.stat().st_mtime).isoformat(),
        "season": season,
        "generated_at": datetime.now().isoformat(),
        "counts_by_position": report.counts_by_position,
        "skipped_row_count": report.skipped_count,
        "apply_injury_discount": apply_injury_discount,
        "players": {k: _statline_to_json(v) for k, v in statlines.items()},
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with cache_file.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    log.info("clay_pdf: wrote cache %s (%d players)", cache_file.name, len(statlines))
    return statlines, report
