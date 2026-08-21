r"""Marc's adjudicated projection decisions: the persistence half of the review queue.

Plan ``docs/PLAN_2026-08-20.md``, "Marc's decisions, round 2". His words: *"I'd like to have
outliers brought to me and highlighted and then we make decisions around whether to boot it or
not."* :mod:`draftroom.valuation.candidates` finds the outliers; this module remembers what he
decided about them and hands the result to
:func:`draftroom.valuation.composite.blend_statlines` as its ``rejected`` argument.

**Nothing in this module ever decides anything.** There is no threshold here, no rule that
promotes a candidate to a rejection, and no default rejection. A number leaves the composite
only because a line in ``data/projection_decisions.json`` says ``"verdict": "reject"``, and that
line only exists because a human wrote it. That is a design constraint with a measured
justification behind it, not a stylistic preference: the scouting sweep found distance-based
auto-rejection is not statistically sound at a small number of correlated sources (the smallest
well-defined symmetric trim of three forecasts is just the median, and Stock & Watson 2004
measured a "drop the worst source" screening rule doing WORSE than plain averaging in three of
six cases), and this repo has since declined two proposed automatic corrections -- the
per-position calibration shrink and identity renormalization -- for failing to beat a dumb null
of the same magnitude. A human deciding case by case is subject to neither objection, because no
rule is being fitted.

THE FILE
--------
``data/projection_decisions.json``, modelled on ``data/overrides.csv``'s discipline: checked
first, permanent, auditable, and editable by hand without running anything. JSON rather than CSV
because a decision carries a free-text reason, and a reason with a comma in it is exactly how a
hand-edited CSV silently loses a column.

    {
      "schema": 1,
      "_note": "...",
      "decisions": [
        {"source": "sleeper", "stat": "*", "player_id": "11638",
         "player_name": "Ricky Pearsall", "verdict": "reject",
         "reason": "all-zero statline carried with games=18", "date": "2026-08-20",
         "detector": "contamination_zero_statline"}
      ]
    }

A bare top-level list is also accepted, because that is what someone hand-editing will
eventually write, and it is what the review page's clipboard export produces. Both shapes
round-trip through :func:`save_decisions` into the dict form.

THE KEY IS ``(source, stat, player_id)``
----------------------------------------
Rejection is per source AND per stat AND per player, because a source is usually wrong about one
thing (a receiver's targets) while fine on everything else, and because an envelope violation
localises to a team rather than to a source-wide stat.

Two deliberate widenings of that grain, both explicit in the file rather than inferred:

* ``"player_id": null`` -- the decision applies to that ``(source, stat)`` for EVERY player.
  This is the grain ``blend_statlines`` accepts natively, and it is the right grain for a
  source-wide defect such as a constant published as a projection.
* ``"stat": "*"`` -- the decision applies to every canonical stat for that player, i.e. "do not
  use this source for this player at all". The right grain for an all-zero statline or a
  suspected wrong-player join, where no single stat is the problem.

HOW IT REACHES THE COMPOSITE
----------------------------
``blend_statlines(rejected=...)`` takes ``Container[tuple[str, str]]`` -- ``(source, stat)``
pairs -- and is called once per player. So the per-player grain is expressed by handing each
call a DIFFERENT container:

    idx = rejected_index(load_decisions())
    blended, prov = blend_statlines(by_source, pos=pos, games_sources=gs,
                                    rejected=idx.for_player(pid))

:meth:`RejectedIndex.for_player` returns a plain ``frozenset`` of ``(source, stat)`` pairs --
the exact type that argument already accepts, with the ``"*"`` sentinel already expanded to
every canonical stat and the source-wide decisions already merged in. Nothing downstream needs
to know this module exists.

LOADING NEVER BREAKS THE BOARD, AND NEVER LIES ABOUT IT EITHER
--------------------------------------------------------------
A missing file means no decisions -- the ordinary state before Marc has reviewed anything, and
the board must build normally. A file that EXISTS but is malformed is a different thing
entirely, and it raises :class:`DecisionsFileError` naming the offending entry by index and
content. Silently ignoring a bad entry would mean a rejection Marc made is quietly not applied,
and he would have no way to tell from the board that his decision had been dropped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from draftroom.prep.schema import CANONICAL_STATS

__all__ = [
    "ALL_STATS",
    "DECISIONS_PATH",
    "KEEP",
    "REJECT",
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "VERDICTS",
    "Decision",
    "DecisionsFileError",
    "RejectedIndex",
    "decisions_note",
    "load_decisions",
    "merge_decisions",
    "new_decision",
    "parse_decisions",
    "rejected_index",
    "rejected_pairs",
    "save_decisions",
]

log = logging.getLogger("draftroom.valuation.decisions")

#: Same anchor ``prep/crosswalk.py`` uses, so both decision files live side by side in ``data/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DECISIONS_PATH = DATA_DIR / "projection_decisions.json"

SCHEMA_VERSION = 1

KEEP = "keep"
REJECT = "reject"
VERDICTS: tuple[str, ...] = (KEEP, REJECT)

#: ``stat`` sentinel meaning "every canonical stat for this player from this source".
ALL_STATS = "*"

#: Fields every entry must carry -- ``player_id`` INCLUDED. ``null`` (source-wide) is a
#: meaningful value and must be written explicitly, because the two readings of an omitted key
#: are wildly different in blast radius: this file is hand-editable by design, and a dropped
#: ``player_id`` line silently promoted one player's rejection into a source-wide one affecting
#: every player that source publishes (Codex 2026-08-21 finding 4). Requiring the key costs one
#: `"player_id": null` in the source-wide case and removes the whole failure mode.
REQUIRED_FIELDS: tuple[str, ...] = ("source", "stat", "verdict", "reason", "date", "player_id")

_FILE_NOTE = (
    "Marc's adjudicated projection decisions (docs/REVIEW_QUEUE.md). Checked first, permanent, "
    "auditable, hand-editable. NOTHING is ever added here automatically: every entry is a human "
    "decision. key = (source, stat, player_id); player_id null means the whole source/stat, "
    "stat '*' means every stat for that player. verdict is 'keep' or 'reject' -- a 'keep' "
    "changes no number and exists so a reviewed-and-accepted outlier stops coming back to the "
    "top of the queue. Later entries win over earlier ones with the same key."
)


def decisions_note() -> str:
    """The ``_note`` written into the file, exposed so a UI can show the same words."""
    return _FILE_NOTE


class DecisionsFileError(ValueError):
    """The decisions file exists but cannot be trusted. Names the offending entry."""


@dataclass(frozen=True)
class Decision:
    """One adjudicated ``(source, stat, player)`` -- keep or reject, with why and when."""

    source: str
    stat: str
    #: ``None`` means the decision applies to that ``(source, stat)`` for every player.
    player_id: str | None
    verdict: str
    reason: str
    date: str
    #: Human-facing only. Never used for matching -- a renamed player must still match on id.
    player_name: str = ""
    #: Which detector surfaced the candidate this decision answers. Audit trail, not logic.
    detector: str = ""

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.source, self.stat, self.player_id)

    @property
    def is_reject(self) -> bool:
        return self.verdict == REJECT

    @property
    def is_source_wide(self) -> bool:
        return self.player_id is None

    @property
    def stats(self) -> tuple[str, ...]:
        """The canonical stats this decision covers, with ``"*"`` expanded."""
        return CANONICAL_STATS if self.stat == ALL_STATS else (self.stat,)

    def as_json(self) -> dict[str, object]:
        return {
            "source": self.source,
            "stat": self.stat,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "verdict": self.verdict,
            "reason": self.reason,
            "date": self.date,
            "detector": self.detector,
        }

    def describe(self) -> str:
        who = self.player_name or self.player_id or "every player"
        scope = "all stats" if self.stat == ALL_STATS else self.stat
        return f"{self.verdict} {self.source}/{scope} for {who} ({self.date}): {self.reason}"


# --------------------------------------------------------------------------------- parsing


def _fail(index: int, entry: object, problem: str) -> None:
    raise DecisionsFileError(
        f"decisions entry #{index} is unusable ({problem}). The offending entry is: "
        f"{json.dumps(entry, default=str)}. Fix it in the file -- a bad entry is NOT skipped, "
        "because a rejection Marc made and this loader quietly dropped would be invisible on "
        "the board."
    )


def _known_sources() -> tuple[str, ...]:
    """The composite's own source list, read at call time rather than imported at module load.

    Imported lazily and defensively on purpose: this module must stay loadable (and the board
    must stay buildable) even while ``composite.py`` is mid-edit, and a source list that has
    grown a fourth family should be picked up without a change here.
    """
    try:
        from draftroom.valuation.composite import SOURCE_PUBLISHES

        return tuple(sorted(SOURCE_PUBLISHES))
    except Exception:  # noqa: BLE001 - never let source-name validation break loading
        log.warning("could not read the composite's source list; source names go unvalidated")
        return ()


def parse_decisions(payload: object) -> tuple[Decision, ...]:
    """Validate and convert a loaded JSON payload (or a JSON string) into decisions.

    Accepts the dict form (``{"schema": 1, "decisions": [...]}``), a bare list of entries, or
    the raw JSON text of either -- the last being what a paste out of the review page's
    clipboard export looks like.

    Raises:
        DecisionsFileError: on any malformed entry, naming it. Never returns a partial list.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise DecisionsFileError(f"not valid JSON: {exc}") from exc

    if isinstance(payload, Mapping):
        entries = payload.get("decisions")
        if entries is None:
            raise DecisionsFileError(
                "decisions payload is an object with no 'decisions' key; expected "
                '{"schema": 1, "decisions": [...]} or a bare list of entries'
            )
        schema = payload.get("schema", SCHEMA_VERSION)
        if schema not in (None, SCHEMA_VERSION):
            raise DecisionsFileError(
                f"decisions file declares schema {schema!r}, but this build understands "
                f"{SCHEMA_VERSION}. Refusing to guess at the difference."
            )
    elif isinstance(payload, Sequence):
        entries = payload
    else:
        raise DecisionsFileError(
            f"decisions payload is a {type(payload).__name__}; expected an object or a list"
        )

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise DecisionsFileError("'decisions' must be a list")

    known = _known_sources()
    out: list[Decision] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            _fail(i, entry, f"not an object but a {type(entry).__name__}")
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            _fail(i, entry, f"missing required field(s) {missing}")

        source = str(entry["source"]).strip()
        stat = str(entry["stat"]).strip()
        verdict = str(entry["verdict"]).strip().lower()
        reason = str(entry["reason"]).strip()
        date = str(entry["date"]).strip()
        # Present by construction: player_id is in REQUIRED_FIELDS. Explicit null (or an empty
        # string) is the documented "this applies to the whole source/stat" form.
        raw_pid = entry["player_id"]
        player_id = None if raw_pid in (None, "", "null") else str(raw_pid).strip()

        if verdict not in VERDICTS:
            _fail(i, entry, f"verdict {verdict!r} is not one of {list(VERDICTS)}")
        if known and source not in known:
            _fail(i, entry, f"unknown source {source!r}; known sources are {list(known)}")
        if stat != ALL_STATS and stat not in CANONICAL_STATS:
            _fail(
                i,
                entry,
                f"stat {stat!r} is not a canonical stat (or {ALL_STATS!r} for all of them); "
                f"canonical stats are {list(CANONICAL_STATS)}",
            )
        if not reason:
            _fail(i, entry, "empty reason -- a decision with no stated reason is not auditable")
        if not date:
            _fail(i, entry, "empty date")

        out.append(
            Decision(
                source=source,
                stat=stat,
                player_id=player_id,
                verdict=verdict,
                reason=reason,
                date=date,
                player_name=str(entry.get("player_name") or "").strip(),
                detector=str(entry.get("detector") or "").strip(),
            )
        )
    return tuple(out)


def load_decisions(path: Path | None = None) -> tuple[Decision, ...]:
    """Read ``data/projection_decisions.json``. A MISSING file means no decisions.

    A missing file is the ordinary pre-review state and must never break a board build. Anything
    else -- unreadable, malformed, or present-but-empty -- is NOT ordinary and raises
    :class:`DecisionsFileError`. This asymmetry is the point: failing open here would quietly
    un-apply human decisions, which is the one degradation this module must never do.
    """
    p = Path(path) if path is not None else DECISIONS_PATH
    if not p.exists():
        return ()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecisionsFileError(f"cannot read {p}: {exc}") from exc
    if not text.strip():
        # ONLY a missing path means "no decisions". A file that exists but is empty is the shape
        # of a truncated write or an interrupted hand-edit, and treating it as "no decisions"
        # silently stopped applying every rejection Marc had made (Codex 2026-08-21 finding 4).
        raise DecisionsFileError(
            f"{p} exists but is empty. An empty file is not the same as no decisions -- it is "
            "what a truncated write looks like. Delete the file to mean 'no decisions', or "
            "restore its contents."
        )
    try:
        return parse_decisions(text)
    except DecisionsFileError as exc:
        raise DecisionsFileError(f"{p}: {exc}") from exc


def merge_decisions(
    existing: Iterable[Decision], incoming: Iterable[Decision]
) -> tuple[Decision, ...]:
    """``incoming`` wins on a shared key; everything else keeps its original order.

    A re-decision is a legitimate act (Marc rejects a number in August and keeps it in
    September), so a duplicate key is not an error -- the later entry simply replaces the
    earlier one, and the file never grows two contradictory lines for the same key.
    """
    by_key: dict[tuple[str, str, str | None], Decision] = {}
    order: list[tuple[str, str, str | None]] = []
    for d in (*existing, *incoming):
        if d.key not in by_key:
            order.append(d.key)
        by_key[d.key] = d
    return tuple(by_key[k] for k in order)


def save_decisions(decisions: Iterable[Decision], path: Path | None = None) -> Path:
    """Write the file atomically (temp + ``os.replace``), the way this repo writes any state."""
    p = Path(path) if path is not None else DECISIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA_VERSION,
        "_note": _FILE_NOTE,
        "decisions": [d.as_json() for d in decisions],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ------------------------------------------------------------------- the composite's input


@dataclass(frozen=True)
class RejectedIndex:
    """The ``rejected`` argument :func:`blend_statlines` wants, per player.

    ``source_wide`` holds ``(source, stat)`` pairs rejected for everybody;
    ``by_player[pid]`` holds the pairs rejected for that one player. :meth:`for_player` is the
    only thing a caller normally needs.
    """

    source_wide: frozenset[tuple[str, str]]
    by_player: Mapping[str, frozenset[tuple[str, str]]]
    #: Every reject decision, keyed the same way, so a UI badge can say WHY a number is gone.
    reasons: Mapping[tuple[str, str, str | None], Decision]

    def for_player(self, player_id: str | None) -> frozenset[tuple[str, str]]:
        """The exact ``frozenset[(source, stat)]`` to pass as ``rejected=`` for this player."""
        if player_id is None:
            return self.source_wide
        specific = self.by_player.get(str(player_id))
        if not specific:
            return self.source_wide
        return self.source_wide | specific

    @classmethod
    def empty(cls) -> "RejectedIndex":
        """Nothing rejected. A real value, not a None sentinel, so callers can always call
        :meth:`for_player` without a null check."""
        return cls(source_wide=frozenset(), by_player={}, reasons={})

    @property
    def is_empty(self) -> bool:
        return not self.source_wide and not self.by_player

    @property
    def n_rejections(self) -> int:
        return len(self.reasons)

    def decisions_for(self, player_id: str | None) -> tuple[Decision, ...]:
        """The reject decisions that apply to this player, source-wide ones included."""
        pid = None if player_id is None else str(player_id)
        return tuple(d for key, d in self.reasons.items() if key[2] is None or key[2] == pid)


def rejected_index(decisions: Iterable[Decision]) -> RejectedIndex:
    """Build the per-player ``rejected`` lookup from a list of decisions.

    ``keep`` decisions are carried in the file but contribute nothing here -- a keep is the
    absence of a rejection, and materialising it as anything else would be a way for a "keep" to
    accidentally change a number.
    """
    source_wide: set[tuple[str, str]] = set()
    by_player: dict[str, set[tuple[str, str]]] = {}
    reasons: dict[tuple[str, str, str | None], Decision] = {}
    for d in decisions:
        if not d.is_reject:
            continue
        reasons[d.key] = d
        for stat in d.stats:
            if d.player_id is None:
                source_wide.add((d.source, stat))
            else:
                by_player.setdefault(d.player_id, set()).add((d.source, stat))
    return RejectedIndex(
        source_wide=frozenset(source_wide),
        by_player={pid: frozenset(pairs) for pid, pairs in by_player.items()},
        reasons=reasons,
    )


def rejected_pairs(
    decisions: Iterable[Decision], player_id: str | None = None
) -> frozenset[tuple[str, str]]:
    """One-shot convenience: :func:`rejected_index` then :meth:`RejectedIndex.for_player`.

    Building the index once is cheaper across a whole board.
    """
    return rejected_index(decisions).for_player(player_id)


def new_decision(
    *,
    source: str,
    stat: str,
    player_id: str | None,
    verdict: str,
    reason: str,
    player_name: str = "",
    detector: str = "",
    date: str | None = None,
) -> Decision:
    """A decision stamped with today's date unless one is supplied."""
    return Decision(
        source=source,
        stat=stat,
        player_id=player_id,
        verdict=verdict,
        reason=reason,
        date=date or _date.today().isoformat(),
        player_name=player_name,
        detector=detector,
    )
