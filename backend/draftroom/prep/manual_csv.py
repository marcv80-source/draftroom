"""Manual CSV ingest adapter: FantasyPros projections, downloaded by hand.

WHY MANUAL, NOT API (Marc's call, 2026-08-17): FantasyPros API access needs
manual approval, premium API keys require a $22.99/mo HOF subscription, and
the free tier may return only sample data. The projections tables are public
web pages with a CSV export. Downloading four CSVs by hand once a week is
less work than maintaining an authenticated client.

This module makes ZERO network calls. No API client, no scraping
fantasypros.com -- it only reads files Marc has already saved to
`data/manual/`. See docs/MANUAL_PROJECTIONS.md for the exact click path he
follows to produce those files.

FILENAME CONVENTION -- two accepted forms:

1. NATIVE (primary, documented path for Marc): FantasyPros' own export
   button produces `FantasyPros_Fantasy_Football_Projections_<POS>.csv`
   (e.g. `FantasyPros_Fantasy_Football_Projections_WR.csv`) with no season
   or date in the name -- Marc drops the four files straight into
   `data/manual/` with **no renaming step**, because a rename is exactly the
   thing a human forgets at 11pm before a draft. Staleness for this form is
   judged off the file's filesystem **modification time** (the moment it was
   downloaded), not a filename date. Season cannot be checked from a native
   filename (there's nothing in it) -- see load_position()'s docstring.
2. DATED (optional alternate, e.g. for archiving multiple snapshots side by
   side): `fantasypros_<pos>_<season>_<YYYY-MM-DD>.csv`, e.g.
   `fantasypros_wr_2026_2026-08-17.csv`. Season and download date come from
   the filename, not mtime.

Both forms also accept a `.tsv` extension (a raw clipboard paste saved from a
text editor is tab-delimited) -- the delimiter is sniffed per file, not
assumed from the extension.

Per position, the newest file across BOTH forms (by download date -- mtime
for native, filename date for dated) is used, and its name is always
reported so a stale or wrong file is visible wherever this output shows up.

FANTASYPROS COLUMN LAYOUT -- THE DUPLICATE-HEADER TRAP:
FantasyPros' half-PPR projections export repeats the `YDS` and `TDS` header
text for two different stat blocks (e.g. WR: receiving YDS/TDS, then rushing
YDS/TDS). The header TEXT is therefore useless for disambiguation --
`csv.DictReader` would silently collapse the two `YDS` columns into one,
dropping either rushing or receiving yards depending on dict-key overwrite
order. This module never uses DictReader for that reason: every row is read
positionally (`csv.reader`, not `csv.DictReader`) against a known, hardcoded
per-position column layout (`POSITION_LAYOUTS` below), and the header row is
still checked -- not to derive the mapping, but to fail loudly if
FantasyPros' actual columns ever drift from what this module assumes.

Every layout is `Player, Team, <stat columns...>, FL, FPTS` -- confirmed
against the real exported files (verified 2026-08-17, `data/manual/`):
    QB: Player,Team,ATT,CMP,YDS,TDS,INTS,ATT,YDS,TDS,FL,FPTS  (passing, then rushing)
    RB: Player,Team,ATT,YDS,TDS,REC,YDS,TDS,FL,FPTS           (rushing, then receiving)
    WR: Player,Team,REC,YDS,TDS,ATT,YDS,TDS,FL,FPTS           (receiving, then rushing)
    TE: Player,Team,REC,YDS,TDS,FL,FPTS                       (receiving only, no rush block)
`Team` is kept (not discarded) because duplicate player names across teams
are a real crosswalk hazard; every StatLine is keyed `"<Player>|<Team>"`, the
same `name|team` convention `prep/crosswalk.py` already uses for FFC rows.

FPTS is discarded on every row, deliberately: see `_FPTS_DISCARD_NOTE`.

FantasyPros does NOT project targets (confirmed against the real column list
in the WR table). No target column exists in any of the four layouts below;
targets come from nflreadpy historical data elsewhere in the pipeline, not
from this adapter.

HOW MARC ACTUALLY PRODUCES THE FILE, AND WHY THE PARSER TOLERATES MESS:
FantasyPros' projections pages have a real CSV export button -- that is now
the documented path (docs/MANUAL_PROJECTIONS.md). The reader still tolerates
a manual copy-paste-into-Excel fallback (comma OR tab delimited, sniffed per
file, not assumed), because either path -- and the real export itself --
can carry artifacts that are NOT a layout change and must not be treated as
one:
  - a junk second line with a mojibake-looking non-breaking space (U+00A0)
    and far fewer fields than the header (confirmed in every real export,
    2026-08-17) -- this and any other row whose Player field is blank once
    stripped is skipped, never becomes a "player",
  - trailing blank lines at end of file (confirmed: the real exports end
    with two bare `""` lines),
  - a stray blank leading (and/or trailing) column from an Excel-paste
    fallback,
  - the header row appearing a second time mid-file,
  - any other footnote-shaped row that doesn't have the right column count
    or contains non-numeric text in a stat column.
All of these are detected and skipped (with a log line) rather than failing
the whole load. What is NOT tolerated, and still fails loudly, is the header
not matching the known per-position layout at all (real column drift) -- see
UnmappedColumnError.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from draftroom.prep.schema import StatLine

log = logging.getLogger("draftroom.prep.manual_csv")

SOURCE = "fantasypros_manual"

# backend/draftroom/prep/manual_csv.py -> parents[3] == repo root (C:\dev\draftroom),
# same depth as prep/http.py's REPO_ROOT.
REPO_ROOT = Path(__file__).resolve().parents[3]
MANUAL_DIR = REPO_ROOT / "data" / "manual"

# This league plays a 17-week season (CLAUDE.md). Refuse anything else at the
# call site rather than guessing a threshold; exposed as a parameter, not
# buried, per the task spec.
DEFAULT_STALENESS_DAYS = 10

# FantasyPros' season-long projection export carries no explicit
# games-played column -- rate stats are already season totals with no
# accompanying "expected games" figure.
#
# RESOLVED 2026-08-18 (was a flat DEFAULT_GAMES = 17.0 fabrication): a flat
# full-season default was measurably wrong -- QBs ranked 25-40 actually
# average ~9-10 of 17 games (nflreadpy 2019-2025 regular season, see
# valuation/replacement.py's fitted rank-conditional curves), not 17, and that
# error multiplied straight into every EVoB and into how fast the man-games
# walk consumed demand. This module now emits NO games value at all for a
# parsed row (the StatLine field is left at its dataclass default, 0.0 --
# never a fabricated positive number). Downstream, `0.0`/absent games is the
# same "no per-player games known" signal `PlayerSeason.expected_games=None`
# already uses to mean "apply the position's rank-conditional prior" (see
# valuation/replacement.py); any code that turns a manual-CSV StatLine into a
# PlayerSeason must pass `expected_games=None` when `statline.games <= 0`,
# never `statline.games` itself, so the prior actually applies instead of
# silently valuing the player at 0 games played.


# Floor check: FantasyPros' real CSV export gives the full table -- confirmed
# live 2026-08-17 at roughly 82 QB / 132 RB / 190 WR / 120 TE rows (see
# data/manual/ and docs/MANUAL_PROJECTIONS.md). These per-position minimums
# are set well under those real counts, deliberately loose, so they catch a
# genuinely truncated file (e.g. a copy-paste that only grabbed a scrolled
# page, or an export that silently changed shape) without false-alarming on
# a real full one. Exposed as a parameter, not buried, so a caller can
# override them.
DEFAULT_MIN_ROWS: dict[str, int] = {"qb": 24, "rb": 40, "wr": 40, "te": 20}

# Primary, documented form: FantasyPros' own export-button filename, as-is,
# no renaming step. No season/date in the name -- staleness is judged off
# mtime instead (see load_position).
NATIVE_FILENAME_RE = re.compile(
    r"^FantasyPros_Fantasy_Football_Projections_(?P<pos>QB|RB|WR|TE)\.(?:csv|tsv)$",
    re.IGNORECASE,
)
# Optional alternate form (e.g. for keeping dated snapshots side by side).
# Season and download date come from the filename itself.
DATED_FILENAME_RE = re.compile(
    r"^fantasypros_(?P<pos>qb|rb|wr|te)_(?P<season>\d{4})_(?P<download_date>\d{4}-\d{2}-\d{2})\.(?:csv|tsv)$",
    re.IGNORECASE,
)

# Leading/trailing blank columns an Excel-HTML-table paste can introduce.
# Deliberately small -- this is tolerance for a known paste artifact, not a
# license to accept an arbitrarily misaligned file.
_MAX_LEADING_BLANK_COLS = 3
_MAX_HEADER_SCAN_ROWS = 10

_FPTS_DISCARD_NOTE = (
    "FPTS is read from the row and thrown away on purpose, never mapped to any "
    "canonical stat. This pipeline re-scores every player from component stats "
    "using the league's own Yahoo stat_modifiers (CLAUDE.md 'Canonical stat "
    "vocabulary'); ingesting FantasyPros' own half-PPR point total would let a "
    "foreign scoring system leak into the model and defeats the entire point of "
    "the scoring-reconciliation gate, which exists to prove OUR re-scoring "
    "matches Yahoo -- not that it matches FantasyPros."
)

# ---------------------------------------------------------------------------
# Per-position column layouts. Position in the tuple is the ONLY thing that
# determines meaning -- header text repeats (YDS, TDS, ATT) across different
# stat blocks and is not used to disambiguate. `None` means "read it, discard
# it" (FPTS). The first two columns of every layout are always `Player` and
# `Team`; the parsing loop below special-cases indices 0 and 1 rather than
# looping generically over them, so their `None` marker here is purely
# documentation of "not a canonical stat", not something the loop consults.
#
# Verified 2026-08-17 against the real exported files in data/manual/
# (FantasyPros_Fantasy_Football_Projections_<POS>.csv). If a real download
# ever shows a different header row, load_position() raises loudly
# (UnmappedColumnError) rather than guessing -- update this table
# deliberately, with the real header pasted into the commit, not by trial
# and error.
# ---------------------------------------------------------------------------
POSITION_LAYOUTS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "qb": (
        ("Player", None),
        ("Team", None),
        ("ATT", "pass_att"),
        ("CMP", "pass_cmp"),
        ("YDS", "pass_yd"),
        ("TDS", "pass_td"),
        ("INTS", "pass_int"),
        ("ATT", "rush_att"),
        ("YDS", "rush_yd"),
        ("TDS", "rush_td"),
        ("FL", "fum_lost"),
        ("FPTS", None),
    ),
    "rb": (
        ("Player", None),
        ("Team", None),
        ("ATT", "rush_att"),
        ("YDS", "rush_yd"),
        ("TDS", "rush_td"),
        ("REC", "rec"),
        ("YDS", "rec_yd"),
        ("TDS", "rec_td"),
        ("FL", "fum_lost"),
        ("FPTS", None),
    ),
    "wr": (
        ("Player", None),
        ("Team", None),
        ("REC", "rec"),
        ("YDS", "rec_yd"),
        ("TDS", "rec_td"),
        ("ATT", "rush_att"),
        ("YDS", "rush_yd"),
        ("TDS", "rush_td"),
        ("FL", "fum_lost"),
        ("FPTS", None),
    ),
    "te": (
        ("Player", None),
        ("Team", None),
        ("REC", "rec"),
        ("YDS", "rec_yd"),
        ("TDS", "rec_td"),
        ("FL", "fum_lost"),
        ("FPTS", None),
    ),
}

POSITIONS: tuple[str, ...] = tuple(POSITION_LAYOUTS)


class ManualCsvError(RuntimeError):
    """Base error for the manual CSV adapter. Every failure mode here is loud
    on purpose -- see CLAUDE.md: an unmapped/unexpected column, a wrong
    season, or a stale file must never be silently accepted."""


class NoFileFoundError(ManualCsvError):
    """No CSV file exists for a position under data/manual/.

    Distinct from every other error here because THIS one is meant to be
    caught and degraded gracefully by callers like prep/fetch_all.py (a
    missing source is a normal, expected state -- Marc hasn't downloaded
    that position yet). Every other exception in this module is meant to
    stop you, not be shrugged off.
    """


class SeasonMismatchError(ManualCsvError):
    """The filename's season doesn't match the season this run is configured for."""


class StaleFileError(ManualCsvError):
    """The newest file for a position is older than the staleness threshold.

    This is the guard against the characteristic manual-ingest failure: a
    file that parses perfectly and is three weeks old, quietly poisoning
    every downstream number. Never downgrade this to a warning.
    """


class UnmappedColumnError(ManualCsvError):
    """The CSV's header row doesn't match the known per-position layout
    (after tolerating a small number of leading/trailing blank columns). A
    hard failure, never a silent skip -- this is real column drift, not a
    footnote row (those are skipped individually; see _parse_position_csv)."""


class TooFewRowsError(ManualCsvError):
    """Parsed row count for a position fell below the plausible floor.

    Catches a truncated copy-paste (e.g. only a JS-paginated teaser table
    got copied) that would otherwise parse cleanly and silently thin the
    player pool. See DEFAULT_MIN_ROWS.
    """


@dataclass(frozen=True)
class ManualFile:
    """One discovered manual CSV, with its metadata.

    `season` is None for a native FantasyPros filename (it carries no season
    marker at all) and an int for the optional dated alternate form.
    `download_date` is the file's mtime for native files, the filename's
    embedded date for dated ones -- either way, "when this was downloaded"
    for the staleness guard.
    """

    position: str
    path: Path
    season: int | None
    download_date: date

    @property
    def name(self) -> str:
        return self.path.name

    def age_days(self, as_of: date) -> int:
        return (as_of - self.download_date).days


@dataclass
class ManualLoadResult:
    """The outcome of loading one position's manual CSV -- always carries the
    file's name and download date so a report built on this output can
    surface exactly which file, and how old, without extra plumbing."""

    position: str
    file: ManualFile
    statlines: dict[str, StatLine]
    row_count: int
    as_of: date

    @property
    def age_days(self) -> int:
        return self.file.age_days(self.as_of)

    @property
    def summary(self) -> str:
        return (
            f"{self.position.upper()}: used {self.file.name} "
            f"({self.row_count} rows, downloaded {self.file.download_date.isoformat()}, "
            f"{self.age_days}d old)"
        )


def _identify_file(path: Path) -> tuple[str, int | None, date] | None:
    """Recognize either accepted filename form. Returns (position, season,
    download_date) or None if the filename matches neither form."""
    m = NATIVE_FILENAME_RE.match(path.name)
    if m:
        pos = m.group("pos").lower()
        mtime_date = datetime.fromtimestamp(path.stat().st_mtime).date()
        return pos, None, mtime_date

    m = DATED_FILENAME_RE.match(path.name)
    if m:
        pos = m.group("pos").lower()
        season = int(m.group("season"))
        download_date = datetime.strptime(m.group("download_date"), "%Y-%m-%d").date()
        return pos, season, download_date

    return None


def find_latest_file(position: str, manual_dir: Path = MANUAL_DIR) -> ManualFile | None:
    """Return the newest ManualFile for `position`, or None if none exists.

    "Newest" is by download date -- mtime for a native FantasyPros filename,
    the filename's own embedded date for the optional dated alternate form
    -- never by re-checking filesystem mtime for the dated form (mtimes get
    reset by copying/syncing, which is exactly why that form embeds its own
    date). Never raises for a missing file -- "missing" is a normal state a
    caller decides how to handle (see NoFileFoundError's docstring and
    prep/fetch_all.py's graceful-degrade wiring).
    """
    pos = position.lower()
    if not manual_dir.exists():
        return None
    candidates: list[ManualFile] = []
    for p in manual_dir.iterdir():
        if not p.is_file():
            continue
        identified = _identify_file(p)
        if identified is None:
            continue
        file_pos, season, download_date = identified
        if file_pos != pos:
            continue
        candidates.append(ManualFile(position=pos, path=p, season=season, download_date=download_date))
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f.download_date, f.path.name))
    return candidates[-1]


def _to_float(value: str) -> float:
    v = value.strip().replace(",", "")
    if not v:
        return 0.0
    return float(v)


def _sniff_delimiter(sample_line: str) -> str:
    """Tab-delimited (raw clipboard paste into a text editor) vs comma
    (Excel Save-As-CSV). Whichever character appears more in the first
    non-blank line wins; comma is the default on a tie/no data."""
    tab_count = sample_line.count("\t")
    comma_count = sample_line.count(",")
    return "\t" if tab_count > comma_count else ","


def _read_rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    first_nonblank = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = _sniff_delimiter(first_nonblank)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _strip_leading_blanks(row: list[str], n: int) -> list[str]:
    if n <= 0 or len(row) < n:
        return row
    if all(not row[i] for i in range(n)):
        return row[n:]
    return row


def _strip_excess_trailing_blanks(row: list[str], target_len: int) -> list[str]:
    """Trim trailing blank cells (Excel-paste padding), but never below
    `target_len` -- a genuinely blank last field (e.g. no FPTS consensus yet)
    must not get mistaken for padding and cause a correct row to look like a
    column-count mismatch."""
    out = list(row)
    while len(out) > target_len and out and not out[-1]:
        out.pop()
    return out


def _find_header(
    rows: list[list[str]], expected_headers: list[str], path_name: str, position: str,
) -> tuple[int, int]:
    """Scan the first few rows for one matching `expected_headers`, tolerating
    a small number of leading blank columns (an Excel-paste artifact -- see
    module docstring). Returns (header_row_index, leading_blank_count).
    Raises UnmappedColumnError if nothing in the scan window matches -- that
    is real column drift, not a paste artifact, and must not be guessed at.
    """
    max_scan = min(_MAX_HEADER_SCAN_ROWS, len(rows))
    for i in range(max_scan):
        cleaned = [c.strip() for c in rows[i]]
        for blanks in range(_MAX_LEADING_BLANK_COLS + 1):
            candidate = _strip_leading_blanks(cleaned, blanks)
            if candidate[: len(expected_headers)] == expected_headers:
                return i, blanks
    raise UnmappedColumnError(
        f"{path_name}: no row in the first {max_scan} lines matches the known "
        f"{position.upper()} FantasyPros layout (allowing up to "
        f"{_MAX_LEADING_BLANK_COLS} leading blank columns).\n"
        f"  expected: {expected_headers}\n"
        f"  saw: {rows[:max_scan]!r}\n"
        "This is a hard failure (CLAUDE.md: an unmapped column is never a silent "
        "skip). If FantasyPros changed their export columns, update "
        "POSITION_LAYOUTS in manual_csv.py deliberately, with the real header "
        "pasted into the commit -- don't guess a fix."
    )


def _parse_position_csv(path: Path, position: str) -> dict[str, StatLine]:
    layout = POSITION_LAYOUTS[position]
    expected_headers = [h for h, _ in layout]

    rows = _read_rows(path)
    if not rows:
        raise ManualCsvError(f"{path.name}: file is empty, no header row")

    header_idx, leading_blanks = _find_header(rows, expected_headers, path.name, position)

    out: dict[str, StatLine] = {}
    for row_num, raw_row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if not raw_row or not any(cell.strip() for cell in raw_row):
            continue  # blank line, e.g. trailing newline

        row = _strip_excess_trailing_blanks(
            _strip_leading_blanks([c.strip() for c in raw_row], leading_blanks), len(layout)
        )

        if row == expected_headers:
            # The header repeated mid-file (a known Excel-paste artifact, e.g.
            # a second table copied into the same clipboard selection) -- not
            # a data row.
            log.debug("%s: skipping repeated header row %d", path.name, row_num)
            continue

        if len(row) != len(layout):
            # Footnote / "Consensus update: <date>" / stray text row -- these
            # come through with the wrong column count, not the right count
            # with wrong values, so a length mismatch here is treated as an
            # artifact rather than real column drift (that was already ruled
            # out by _find_header matching the header row itself).
            log.warning(
                "%s: skipping row %d that doesn't match the %s column count "
                "(likely a footnote/paste artifact, not a player row): %r",
                path.name, row_num, position.upper(), raw_row,
            )
            continue

        # Columns 0 and 1 are always Player, Team (see POSITION_LAYOUTS). A
        # blank/whitespace/NBSP-only Player field is the junk row every real
        # export carries on line 2 -- str.strip() already normalized NBSP
        # (U+00A0) to nothing above, so this also catches it even though the
        # length check would have already caught that specific case.
        name = row[0]
        if not name:
            continue
        team = row[1] if len(row) > 1 else ""

        try:
            # No "games" key here on purpose -- see the note above DEFAULT_GAMES' removal:
            # StatLine.games stays at its dataclass default (0.0), which downstream code must
            # read as "unknown, apply the positional prior," never as "projected for 0 games."
            kwargs: dict[str, float] = {}
            for (header, canonical), value in zip(layout[2:], row[2:]):
                if canonical is None:
                    continue  # FPTS (see _FPTS_DISCARD_NOTE)
                kwargs[canonical] = _to_float(value)
        except ValueError:
            log.warning(
                "%s: skipping row %d with a non-numeric stat value (likely a "
                "footnote row disguised at the right column count): %r",
                path.name, row_num, raw_row,
            )
            continue

        # Keyed "Player|Team", matching the name|team|pos convention
        # prep/crosswalk.py already uses for FFC rows (position is implicit
        # per-file here) -- duplicate player names across teams are a real
        # hazard and Team is exactly the disambiguator crosswalk needs.
        out[f"{name}|{team}"] = StatLine(**kwargs)
    return out


def load_position(
    position: str,
    *,
    season: int,
    manual_dir: Path = MANUAL_DIR,
    max_age_days: int = DEFAULT_STALENESS_DAYS,
    min_rows: int | None = None,
    as_of: date | None = None,
) -> ManualLoadResult:
    """Load the newest manual CSV for `position`, mapped into canonical stats.

    Raises (never silently degrades):
      NoFileFoundError    -- no file at all for this position. Callers that
                              want graceful multi-source degradation (e.g.
                              prep/fetch_all.py) should catch this one
                              specifically.
      SeasonMismatchError -- the file's embedded season != `season`. Only
                              checked for the optional dated filename form --
                              FantasyPros' native export filename carries no
                              season marker at all, so this check is a no-op
                              for it; the staleness guard (mtime-based for
                              that form) is the real defense against a
                              wrong-season file going stale unnoticed.
      StaleFileError      -- the file is older than `max_age_days` (default
                              DEFAULT_STALENESS_DAYS = 10). Exposed as a
                              parameter deliberately, per the task spec, so a
                              caller can tighten or loosen it without editing
                              this module.
      UnmappedColumnError -- header doesn't match the known per-position
                              layout (real column drift, not a paste artifact).
      TooFewRowsError     -- parsed row count fell below `min_rows` (default
                              DEFAULT_MIN_ROWS[position]) -- catches a
                              truncated copy-paste.

    `as_of` defaults to today; tests pass an explicit date so staleness
    checks are deterministic and never depend on wall-clock time.
    """
    pos = position.lower()
    if pos not in POSITION_LAYOUTS:
        raise ManualCsvError(f"unknown position {position!r}; expected one of {sorted(POSITION_LAYOUTS)}")

    latest = find_latest_file(pos, manual_dir=manual_dir)
    if latest is None:
        raise NoFileFoundError(
            f"no manual CSV found for position {pos.upper()} under {manual_dir}. "
            f"Expected 'FantasyPros_Fantasy_Football_Projections_{pos.upper()}.csv' "
            f"(FantasyPros' own export filename, no renaming needed) or the optional "
            f"dated form 'fantasypros_{pos}_{season}_YYYY-MM-DD.csv'. "
            "See docs/MANUAL_PROJECTIONS.md for the download steps."
        )

    if latest.season is not None and latest.season != season:
        raise SeasonMismatchError(
            f"{latest.name}: file season {latest.season} does not match the configured "
            f"season {season}. Refusing to load a wrong-season file silently -- "
            f"re-download the {season} projections (see docs/MANUAL_PROJECTIONS.md)."
        )

    today = as_of if as_of is not None else datetime.now().date()
    age = latest.age_days(today)
    if age > max_age_days:
        raise StaleFileError(
            f"{latest.name}: downloaded {latest.download_date.isoformat()} is {age} days "
            f"old, older than the {max_age_days}-day staleness threshold. A stale file "
            "that parses perfectly is the exact failure mode this guard exists to catch "
            f"-- re-download fresh {pos.upper()} projections before trusting this "
            "snapshot. See docs/MANUAL_PROJECTIONS.md."
        )

    statlines = _parse_position_csv(latest.path, pos)

    floor = DEFAULT_MIN_ROWS[pos] if min_rows is None else min_rows
    if len(statlines) < floor:
        raise TooFewRowsError(
            f"{latest.name}: parsed only {len(statlines)} {pos.upper()} rows, below the "
            f"floor of {floor}. A real export should be well over 100 rows for this "
            "position -- re-export the CSV from the FantasyPros projections page (see "
            "docs/MANUAL_PROJECTIONS.md) rather than trusting a thin file."
        )

    result = ManualLoadResult(
        position=pos, file=latest, statlines=statlines, row_count=len(statlines), as_of=today,
    )
    log.info("manual_csv: %s", result.summary)
    return result


def load_all_positions(
    *,
    season: int,
    manual_dir: Path = MANUAL_DIR,
    max_age_days: int = DEFAULT_STALENESS_DAYS,
    min_rows: dict[str, int] | None = None,
    as_of: date | None = None,
) -> dict[str, ManualLoadResult]:
    """Load every position that has a file. Missing positions are silently
    omitted from the returned dict -- this is the graceful-degrade behavior
    callers like prep/fetch_all.py build a warning on top of; every OTHER
    failure (season mismatch, stale, bad columns, too-few-rows) still raises."""
    out: dict[str, ManualLoadResult] = {}
    for pos in POSITIONS:
        pos_min_rows = None if min_rows is None else min_rows.get(pos)
        try:
            out[pos] = load_position(
                pos, season=season, manual_dir=manual_dir, max_age_days=max_age_days,
                min_rows=pos_min_rows, as_of=as_of,
            )
        except NoFileFoundError:
            continue
    return out
