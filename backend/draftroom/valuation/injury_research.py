r"""Externally researched availability findings: the file, and the notes the board shows.

WHY THIS MODULE EXISTS, AND WHY IT IS IN ``backend/`` RATHER THAN ``tools/``
---------------------------------------------------------------------------
``data/injury_research.json`` is written by the final-prep availability job
(``docs/FINAL_PREP.md``) and was, until this module, read by exactly one consumer:
``tools/injury_sweep.py``, which turns a finding into a games override or a contamination
rejection. That consumer is PREP-phase, and it can only act on a finding that carries a NUMBER.

So a finding shaped *"we know something is wrong and nobody can put a number on it"* had
nowhere to go. It produced no override, so ``applied_playing_time`` never saw it, so no badge
rendered, and the only record of it was a JSON file nobody opens in a live room. That is the
exact category the runbook itself calls out as having **zero sources** -- suspension, discipline,
a roster decision that has not happened yet -- which makes it the category most likely to
surprise Marc at the table and the LEAST likely to be caught by anything automatic.

This module is therefore the canonical loader, importable from the draft phase, offline. The
prep tool imports ``Finding`` and ``load_research`` from here rather than defining its own, so
there is one parser and one schema instead of two that can drift.

``games_missed: null`` MEANS "UNPRICED", AND IT IS NOT THE SAME AS ZERO
-----------------------------------------------------------------------
The distinction is load-bearing and is the whole reason the schema grew a nullable field:

* ``games_missed: 0`` is a CLAIM -- he will play the full season. It is a finding with a number
  in it, and the sweep is right to propose nothing.
* ``games_missed: null`` is an ABSENCE of a claim -- something is known (an open disciplinary
  review, a camp battle he may lose, a trade rumour) and no honest games figure exists for it.

Collapsing the second into the first would be the invented number this repo has twice declined
to ship (CLAUDE.md: every correction must beat a dumb null of equal magnitude; nothing here
asserts what a designation costs). Collapsing it the other way -- refusing to record it at all --
is what the code did before, and it lost the finding entirely.

An unpriced finding therefore changes NO number anywhere. It is carried to the board as a note
and rendered as a badge, and the decision stays Marc's, in the room, with the citation in front
of him.

WHICH FINDINGS BECOME NOTES: THE SAME ASYMMETRY ``REJ`` ALREADY USES
--------------------------------------------------------------------
A note is shown for research that is **not already reflected in a number**. Concretely,
:func:`unpriced_notes` filters out any player who has an APPLIED playing-time override, because
for him the ``NN.NG`` badge is the better, more specific statement -- it says what the research
cost, rather than that research exists. The note is what remains when the pipeline could not
price the finding.

That means a note is expected to DISAPPEAR when Marc applies an override for that player, and
that is correct rather than a regression: it has been replaced by a stronger badge. It mirrors
CLAUDE.md's standing rule that a badge must never claim a change that did not happen, run in the
other direction -- a badge must not stay silent about a judgement that is NOT in the numbers.

LOADING FAILS CLOSED, IDENTICALLY TO ITS TWO SIBLINGS
------------------------------------------------------
Same rule as :mod:`draftroom.valuation.decisions` and :mod:`draftroom.valuation.playing_time`,
for the same reason. A MISSING file means nothing was researched, which is an ordinary state and
must build normally. A file that EXISTS but is empty or malformed raises
:class:`InjuryResearchError` naming the offending entry -- an empty file is what a truncated
write looks like, and reading it as "nothing researched" would silently drop every finding of a
research session while the board kept looking fine.

``player_id`` IS THE SLEEPER ID AND MAY NEVER BE NULL
------------------------------------------------------
Two different id spaces are both called ``player_id`` (CLAUDE.md). This file, like
``playing_time.json`` and ``projection_decisions.json``, uses the **Sleeper** id.
``PoolPlayer.player_id`` is FFC-derived and is a different number for the same man. An entry
written against the wrong one binds to nobody and fails silently.

Null is refused rather than reinterpreted: ``decisions.py`` gives null a real meaning (the
decision applies source-wide), and an availability finding is a fact about ONE player with no
such grain.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "RESEARCH_PATH",
    "SCHEMA_VERSION",
    "Finding",
    "InjuryResearchError",
    "ResearchNote",
    "findings_by_pid",
    "load_research",
    "parse_research",
    "unpriced_notes",
]

log = logging.getLogger("draftroom.valuation.injury_research")

#: Same anchor the sibling decision files use, so all three live side by side in ``data/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCH_PATH = REPO_ROOT / "data" / "injury_research.json"

#: Matching ``playing_time.py``. A file declaring a version this code does not understand is
#: refused rather than read under this version's assumptions -- the whole point of stamping a
#: version is that a later shape change can be detected instead of silently misparsed.
SCHEMA_VERSION = 1


class InjuryResearchError(ValueError):
    """The research file exists but cannot be trusted. Never degraded to 'no findings'."""


@dataclass(frozen=True)
class Finding:
    """One player's externally researched status. Mirrors the JSON one-for-one."""

    player_id: str
    player_name: str
    status: str
    season_ending: bool
    #: Games this player is expected to MISS. ``None`` means UNPRICED: something is known and no
    #: honest number exists for it. Never conflate with ``0.0``, which is a positive claim that
    #: he will play the full season. See the module docstring.
    games_missed: float | None
    confidence: str
    report_date: str
    citation: str
    notes: str = ""

    @property
    def is_severe(self) -> bool:
        """Season over, by the explicit flag. No threshold here."""
        return self.season_ending

    @property
    def is_unpriced(self) -> bool:
        """True when the research carries no games figure at all -- the note-only case."""
        return self.games_missed is None

    def as_note_payload(self) -> dict[str, Any]:
        """Flattened for the API payload. The citation and date travel with the claim."""
        return {
            # The name the RESEARCH names, which is not necessarily the name on the row. A valid
            # id pointing at the wrong player binds cleanly and silently; surfacing this in the
            # payload means the tooltip can be checked against the row without reading a log.
            "player_name": self.player_name,
            "status": self.status,
            "confidence": self.confidence,
            "report_date": self.report_date,
            "citation": self.citation,
            "notes": self.notes,
            "season_ending": self.season_ending,
            # None here is the signal the UI keys on: research exists, no number came of it.
            "games_missed": self.games_missed,
        }


@dataclass(frozen=True)
class ResearchNote:
    """A finding the board is showing because no number in the pipeline reflects it."""

    player_id: str
    finding: Finding
    #: Why this finding never became a number. Shown to Marc verbatim, because "nobody has
    #: decided yet" and "nobody can decide" are different situations and he acts on them
    #: differently.
    reason: str

    def as_payload(self) -> dict[str, Any]:
        """The finding's own fields plus this module's verdict on why it is unpriced.

        ``notes`` stays the researcher's prose and ``why_unpriced`` is this module's sentence.
        Keeping both matters: the first is evidence, the second is the reason a badge is on
        screen, and collapsing them would make the badge look like something a human wrote.
        """
        payload = self.finding.as_note_payload()
        payload["why_unpriced"] = self.reason
        return payload


def _fail(index: int, entry: object, problem: str) -> None:
    raise InjuryResearchError(
        f"data/injury_research.json entry {index} ({entry!r}): {problem}. "
        "Fix the file rather than deleting the entry -- a dropped line is a decision nobody made."
    )


def parse_research(payload: object) -> tuple[Finding, ...]:
    """Validate the research payload. Strict: this feeds a rejection, an override, and a badge."""
    if isinstance(payload, Mapping):
        declared = payload.get("schema", SCHEMA_VERSION)
        if declared != SCHEMA_VERSION:
            raise InjuryResearchError(
                f"data/injury_research.json declares schema {declared!r}, but this code "
                f"understands schema {SCHEMA_VERSION}. Refusing to read it under the wrong "
                "assumptions -- upgrade the reader or fix the file."
            )
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
        # REQUIRED and never null -- see the module docstring on the two id spaces and on why
        # decisions.py's null grain has no meaning for a fact about one player.
        if not isinstance(pid, str) or not pid.strip():
            _fail(i, entry, "'player_id' must be a non-empty string (never null)")

        # Absent -> 0.0 (the historical default, a positive full-season claim).
        # Explicit null -> None (UNPRICED). These are deliberately different; see the docstring.
        games_missed: float | None
        if "games_missed" not in entry:
            games_missed = 0.0
        elif entry["games_missed"] is None:
            games_missed = None
        else:
            raw = entry["games_missed"]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                _fail(i, entry, "'games_missed' must be a number, or null to mean UNPRICED")
            # NaN and Infinity are ACCEPTED by Python's json parser and survive `raw < 0`
            # (every comparison against NaN is False). Left in, NaN reaches
            # `max(0.0, weeks - NaN)` in the sweep, which returns 0.0 -- so `--apply` would
            # silently write a ZERO-GAMES override for a healthy player, which is the single
            # worst thing this file could do. (Codex 2026-08-27, P1.)
            if not math.isfinite(raw):
                _fail(
                    i,
                    entry,
                    f"'games_missed' must be a finite number (got {raw!r}) -- NaN and Infinity "
                    "survive a negativity check and become a zero-games override downstream",
                )
            if raw < 0:
                _fail(i, entry, "'games_missed' cannot be negative")
            games_missed = float(raw)

        season_ending = entry.get("season_ending", False)
        if not isinstance(season_ending, bool):
            _fail(i, entry, "'season_ending' must be true or false, not a string")
        if season_ending and games_missed is None:
            # A season-ending finding IS a number (zero games played). Allowing it to be unpriced
            # would let the most actionable finding in the file render as a soft note.
            _fail(
                i,
                entry,
                "'season_ending' is true but 'games_missed' is null -- a season-ending finding "
                "is a games figure, not an unpriced one",
            )
        for required in ("report_date", "citation"):
            if not str(entry.get(required, "")).strip():
                _fail(
                    i,
                    entry,
                    f"'{required}' is required -- an availability claim with no source and no "
                    "date is exactly the unverifiable input this file exists to prevent",
                )
        out.append(
            Finding(
                player_id=str(pid).strip(),
                player_name=str(entry.get("player_name", "")).strip(),
                status=str(entry.get("status", "")).strip(),
                season_ending=season_ending,
                games_missed=games_missed,
                confidence=str(entry.get("confidence", "")).strip().upper(),
                report_date=str(entry.get("report_date", "")).strip(),
                citation=str(entry.get("citation", "")).strip(),
                notes=str(entry.get("notes", "")).strip(),
            )
        )
    return tuple(out)


def load_research(path: Path | None = None) -> tuple[Finding, ...]:
    """Missing file -> no findings. Present but broken -> raise. Same rule as the sibling files.

    EVERY way a present file can fail to read is an ``InjuryResearchError``, not just bad JSON.
    Wrapping only :class:`json.JSONDecodeError` left two real holes (Codex 2026-08-27, P1): an
    interrupted write that ends mid-multibyte-character raises ``UnicodeDecodeError``, and a
    locked or permission-denied file raises ``OSError``. Both would escape this function as a
    generic exception, land in ``live_data``'s broad handler, and degrade the app to
    ADP-placeholder mode with ``/healthz`` still returning 200 -- which reads as "the cache is
    stale" rather than "your researched findings stopped being shown". That is precisely the
    failure the fail-closed rule exists to prevent, and it has now been fixed once for each of
    the three decision files.
    """
    path = path or RESEARCH_PATH
    if not path.exists():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InjuryResearchError(f"{path} exists but could not be read: {exc}") from exc
    except UnicodeError as exc:
        raise InjuryResearchError(
            f"{path} is not valid UTF-8: {exc}. A file that ends inside a multibyte character "
            "is what an interrupted write looks like."
        ) from exc
    if not text.strip():
        # A zero-byte or whitespace-only file is the classic truncated write. It is NOT valid
        # JSON either, but saying so explicitly beats a parser error nobody can act on.
        raise InjuryResearchError(
            f"{path} exists but is empty. An empty file is what a truncated write looks like; "
            "delete the file entirely to mean 'nothing researched'."
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InjuryResearchError(f"{path} is not valid JSON: {exc}") from exc
    return parse_research(payload)


def findings_by_pid(findings: Sequence[Finding]) -> dict[str, Finding]:
    """Last entry wins, matching how the sibling files resolve a duplicated ``player_id``."""
    return {f.player_id: f for f in findings}


def unpriced_notes(
    findings: Sequence[Finding],
    *,
    priced_pids: Sequence[str] | set[str] = (),
) -> dict[str, ResearchNote]:
    """Findings the board must show BECAUSE no number in the pipeline reflects them.

    Two shapes qualify, and they are reported with different ``reason`` text because Marc acts
    on them differently:

    * **unpriced** -- ``games_missed`` is null. Nothing could price it; nothing ever will
      automatically. This is the suspension/discipline case with zero sources.
    * **not yet applied** -- the finding carries a real games figure, but no playing-time
      override for that player actually moved the board. Either Marc has not applied it yet
      (Pierce and Charbonnet, deferred to the cutdown by his own call) or the curve clamped it
      away. In both cases the number on the board does not reflect the research.

    ``priced_pids`` is the set of players whose APPLIED override already moved a number --
    normally ``RealBoard.applied_playing_time``. Those are excluded, because for them the games
    badge is the stronger and more specific statement.
    """
    priced = set(priced_pids)
    out: dict[str, ResearchNote] = {}
    for f in findings:
        if f.player_id in priced:
            continue
        if f.is_unpriced:
            reason = (
                "No games figure exists for this finding. Nothing in the pipeline can price it "
                "and no source publishes it -- carried as a note so the decision is yours."
            )
        else:
            missed = f"{f.games_missed:g}"
            reason = (
                f"Research says he misses {missed} game(s), and no playing-time override is "
                "moving the board for him. The value on this row does NOT reflect this finding."
            )
        out[f.player_id] = ResearchNote(player_id=f.player_id, finding=f, reason=reason)
    return out
