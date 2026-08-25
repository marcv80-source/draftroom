r"""Marc's manual playing-time overrides: the one place a human can set expected games.

WHY THIS EXISTS
---------------
``injury_status`` is carried on :class:`~draftroom.live_data.PoolPlayer` and rendered as a badge,
and until this module it touched **nothing** in the valuation. Whether a will-not-play
designation reached ``expected_games`` depended entirely on whether ESPN happened to price it
in, which is accidental rather than principled: measured on the ranked pool 2026-08-20, ESPN
discounted Kittle (15 games) and Charbonnet (11) for PUP but projected Alec Pierce (PUP, ADP
70.3) for a full 17, so the board credited Pierce **15.50 of 17** -- exactly the
rank-conditional availability curve for WR30, i.e. the figure for a player about whom nothing
player-specific is known.

:func:`draftroom.valuation.candidates.detect_injury_vs_expected_games` surfaces that gap and
deliberately marks it ``actionable=False``, because the review queue's only lever is
``blend_statlines(rejected=...)`` and **rejecting a source cannot change an availability
figure**. No source is even at fault: ESPN's 17.0 is an ordinary if-healthy projection, and
"projections are not expectations" is a measured finding in this repo (CLAUDE.md). The missing
lever is this file.

NOTHING HERE DECIDES ANYTHING
-----------------------------
Same constraint as :mod:`draftroom.valuation.decisions`, for the same reason. There is no fitted
model here, no designation-to-games table, and no default. A player's games figure changes only
because a line in ``data/playing_time.json`` says so, and that line exists only because a human
wrote it. See :data:`draftroom.valuation.candidates.NO_EMPIRICAL_DESIGNATION_FIT` for why the
alternative is not available: Sleeper's designation is current-year while the only per-player
games history in the cache is 2025 actuals, so **no games-missed figure for PUP/IR/DNR is
derivable from this repo's data**, and asserting one would be exactly the arbitrary rule this
project has twice declined to ship.

THE FILE
--------
``data/playing_time.json``, modelled on ``data/projection_decisions.json``'s discipline:
checked first, permanent, auditable, hand-editable without running anything.

    {
      "schema": 1,
      "_note": "...",
      "overrides": [
        {"player_id": "8142", "player_name": "Alec Pierce", "games": 11.0,
         "reason": "PUP -- Marc: not expected back before ~week 5",
         "date": "2026-08-24"}
      ]
    }

A bare top-level list is accepted too, because that is what a hand-edit eventually looks like.
Both shapes round-trip through :func:`save_overrides` into the dict form.

THE KEY IS ``player_id`` -- AND IT IS NEVER NULL
------------------------------------------------
Unlike a projection decision, an override has no meaningful wider grain. There is no such thing
as a source-wide playing-time claim: availability is a fact about one player. So ``player_id``
is required AND must be a real id, and the ``null`` form that ``decisions.py`` accepts (with its
own documented reason) is rejected here rather than quietly reinterpreted.

THE SEMANTICS: ``expected_games = min(override, curve(pos, rank))``
------------------------------------------------------------------
The override REPLACES whatever games figure the active source published -- including the ``None``
that FantasyPros and FantasySharks leave behind -- and is then clamped by the same
rank-conditional availability curve that already caps every other player
(:func:`draftroom.validate.board._cap_expected_games_by_curve`).

That clamp is load-bearing in both directions, and it is derived rather than chosen:

* **Downward is passed straight through.** Bad news is the whole point. An override of 11 games
  for a PUP player lands at 11, because the curve figure of 15.50 was never a claim about *him*.
* **Upward stops at the curve.** The curve is FITTED actual availability at that positional
  rank. A human can say "he is fully cleared, ignore the source's 11" and get the player back up
  to the healthy-rank figure, which is a real and useful act. A human cannot push him ABOVE it,
  because that would be claiming better-than-typical durability for a rank on the strength of a
  press report -- and it is the one direction whose error is expensive, since it inflates a
  player Marc would then draft at full value.

The clamp also means this feature weakens **no** gate to get itself admitted:
:func:`draftroom.validate.invariants.check_expected_games_capped_by_curve` stays true by
construction, not by exemption. That was a hard requirement -- an override mechanism that had to
loosen an invariant would be indistinguishable from a bug.

**PPG is untouched.** An override moves the games VOLUME a per-game rate is credited for and
never the rate itself, exactly as ``_cap_expected_games_by_curve`` already documents for the
curve. A view that a player will be *worse* per game is a projection question and belongs in the
review queue, not here.

NO UPPER BOUND IS INVENTED
--------------------------
Validation rejects a negative games figure, because that is definitionally impossible, and
stops there. It deliberately does NOT enforce a maximum: the curve does that, per player, from a
fit, and a second hardcoded ceiling here would be a number nobody derived.

LOADING NEVER BREAKS THE BOARD, AND NEVER LIES ABOUT IT EITHER
--------------------------------------------------------------
Identical asymmetry to :func:`draftroom.valuation.decisions.load_decisions`, for the identical
reason. A MISSING file means no overrides -- the ordinary state, and the board must build
normally. A file that exists but is empty or malformed raises :class:`PlayingTimeFileError`
naming the offending entry, because an empty file is what a truncated write looks like, and
silently reading it as "no overrides" would un-apply a judgement Marc made while the board kept
looking fine.

AN OVERRIDE THAT CHANGES NOTHING IS REPORTED, NOT HIDDEN
--------------------------------------------------------
:func:`bind` returns both the resulting figure and whether the override actually MOVED it. An
inert override (clamped away, or landing on the value the pipeline already had) is a sign that
Marc's note is not doing what he thinks it is, so the board build logs it and the badge does not
claim a change that did not happen -- the same asymmetry the ``REJ`` badge already follows
(CLAUDE.md: "A rejection badges only players whose OWN number changed").
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "OVERRIDES_PATH",
    "REQUIRED_FIELDS",
    "SCHEMA_VERSION",
    "Binding",
    "PlayingTimeFileError",
    "PlayingTimeOverride",
    "bind",
    "load_overrides",
    "merge_overrides",
    "new_override",
    "overrides_by_pid",
    "overrides_note",
    "parse_overrides",
    "save_overrides",
]

log = logging.getLogger("draftroom.valuation.playing_time")

#: Same anchor ``decisions.py`` uses, so both human-decision files live side by side in ``data/``.
REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
OVERRIDES_PATH = DATA_DIR / "playing_time.json"

SCHEMA_VERSION = 1

#: Every entry must carry all four. ``player_id`` is required AND must be non-null: there is no
#: source-wide playing-time claim, so the ``null`` grain ``decisions.py`` supports has no
#: meaning here and is refused rather than reinterpreted.
REQUIRED_FIELDS: tuple[str, ...] = ("player_id", "games", "reason", "date")

_FILE_NOTE = (
    "Marc's playing-time overrides. Checked first, permanent, auditable, hand-editable. "
    "NO ENTRY IS EVER DERIVED FROM THIS REPO'S OWN DATA: no games-missed figure for a PUP/IR/DNR "
    "designation is derivable from the cache (see valuation/candidates.py "
    "NO_EMPIRICAL_DESIGNATION_FIT), so every number here traces to a human decision on outside "
    "information. Two routes write it, and both keep that property: a hand edit, or "
    "tools/injury_sweep.py --apply, which turns EXTERNALLY RESEARCHED and human-approved "
    "reporting in data/injury_research.json into entries whose 'reason' carries the dated "
    "citation behind them. An entry with no citable basis in its reason is a bug in whatever "
    "wrote it. key = player_id. 'games' REPLACES whatever games figure the "
    "active projection source published for him, and is then clamped by the same fitted "
    "rank-conditional availability curve that caps every other player -- so an override can "
    "lower a player freely and can restore him only as far as the healthy-rank figure, never "
    "past it. PPG is never touched: this moves the games VOLUME, not the per-game rate. Later "
    "entries win over earlier ones with the same player_id."
)


def overrides_note() -> str:
    """The ``_note`` written into the file, exposed so a UI can show the same words."""
    return _FILE_NOTE


class PlayingTimeFileError(ValueError):
    """The overrides file exists but cannot be trusted. Names the offending entry."""


@dataclass(frozen=True)
class PlayingTimeOverride:
    """One human claim about one player's availability, with why and when."""

    player_id: str
    #: Games this player is expected to PLAY. Replaces the source's figure; then curve-clamped.
    games: float
    reason: str
    date: str
    #: Human-facing only. Never used for matching -- a renamed player must still match on id.
    player_name: str = ""
    #: The designation (PUP/IR/DNR/...) that prompted this, when one did. Audit trail, not logic:
    #: nothing in this repo asserts what a designation costs, so this never affects the number.
    designation: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "player_id": self.player_id,
            "player_name": self.player_name,
            "games": self.games,
            "reason": self.reason,
            "date": self.date,
            "designation": self.designation,
        }

    def describe(self) -> str:
        who = self.player_name or self.player_id
        tag = f" [{self.designation}]" if self.designation else ""
        return f"{who}{tag} -> {self.games:.2f} games ({self.date}): {self.reason}"


# --------------------------------------------------------------------------------- parsing


def _fail(index: int, entry: object, problem: str) -> None:
    raise PlayingTimeFileError(
        f"playing-time entry #{index} is unusable ({problem}). The offending entry is: "
        f"{json.dumps(entry, default=str)}. Fix it in the file -- a bad entry is NOT skipped, "
        "because an availability judgement Marc made and this loader quietly dropped would be "
        "invisible on the board."
    )


def parse_overrides(payload: object) -> tuple[PlayingTimeOverride, ...]:
    """Validate and convert a loaded JSON payload (or raw JSON text) into overrides.

    Accepts the dict form (``{"schema": 1, "overrides": [...]}``) or a bare list of entries.

    Raises:
        PlayingTimeFileError: on any malformed entry, naming it. Never returns a partial list.
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PlayingTimeFileError(f"not valid JSON: {exc}") from exc

    if isinstance(payload, Mapping):
        entries = payload.get("overrides")
        if entries is None:
            raise PlayingTimeFileError(
                "playing-time payload is an object with no 'overrides' key; expected "
                '{"schema": 1, "overrides": [...]} or a bare list of entries'
            )
        schema = payload.get("schema", SCHEMA_VERSION)
        if schema not in (None, SCHEMA_VERSION):
            raise PlayingTimeFileError(
                f"playing-time file declares schema {schema!r}, but this build understands "
                f"{SCHEMA_VERSION}. Refusing to guess at the difference."
            )
    elif isinstance(payload, Sequence):
        entries = payload
    else:
        raise PlayingTimeFileError(
            f"playing-time payload is a {type(payload).__name__}; expected an object or a list"
        )

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        raise PlayingTimeFileError("'overrides' must be a list")

    out: list[PlayingTimeOverride] = []
    seen: dict[str, int] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            _fail(i, entry, f"not an object but a {type(entry).__name__}")
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            _fail(i, entry, f"missing required field(s) {missing}")

        # NORMALIZE FIRST, THEN VALIDATE. Checking emptiness before stripping let "   " and
        # " null " through as player ids, which then silently missed every board player and
        # degraded to an unmatched-override warning -- i.e. the judgement was never applied
        # (Codex 2026-08-24 finding 4). A hand-editable file WILL contain stray whitespace.
        raw_pid = entry["player_id"]
        if raw_pid is None:
            # Checked FIRST, ahead of the type test, so a literal JSON `null` gets the message
            # that explains the mistake rather than a generic type complaint. Copying a
            # source-wide line out of projection_decisions.json is the likeliest way to write
            # this file wrong, and the error is the only place that can say why it is wrong.
            _fail(
                i,
                entry,
                "player_id is null -- unlike a projection decision, a playing-time override has "
                "no source-wide grain: availability is a fact about one player, so there is "
                "nothing for a null to mean here",
            )
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (str, int)):
            # A float id would round-trip through str() as "8142.0" and match nothing; a list or
            # dict would stringify into something that looks like an id and is not one.
            _fail(
                i,
                entry,
                f"player_id must be a string or integer, not a {type(raw_pid).__name__}",
            )
        player_id = str(raw_pid).strip()
        if player_id.lower() in ("", "null", "none"):
            # The same point as the `is None` branch above, reached via the text forms a
            # hand-edit produces: "", "   ", "null" as a quoted string.
            _fail(
                i,
                entry,
                f"player_id is {raw_pid!r}, which normalises to empty -- unlike a projection "
                "decision, a playing-time override has no source-wide grain: availability is a "
                "fact about one player, so there is nothing for an empty id to mean here",
            )

        raw_games = entry["games"]
        if isinstance(raw_games, bool) or not isinstance(raw_games, (int, float)):
            _fail(i, entry, f"games must be a number, not a {type(raw_games).__name__}")
        games = float(raw_games)
        if not math.isfinite(games):
            _fail(i, entry, f"games is {raw_games!r}, which is not a real number")
        if games < 0:
            # The ONLY bound enforced here. No maximum is invented: the fitted availability
            # curve supplies the ceiling per player, and a second hardcoded one would be a
            # number nobody derived.
            _fail(i, entry, f"games is negative ({games}); a player cannot play fewer than 0")

        # Same trap as player_id, one step worse: `str(None)` is the NONEMPTY string "None", so a
        # `"reason": null` sailed past an emptiness check and applied a valuation change with an
        # unusable audit trail (Codex 2026-08-24 finding 4). Require a real string.
        for field_name in ("reason", "date"):
            if not isinstance(entry[field_name], str):
                _fail(
                    i,
                    entry,
                    f"{field_name} must be a string, not a "
                    f"{type(entry[field_name]).__name__} -- str(None) is the nonempty text "
                    f"'None', which would pass an emptiness check and leave this override "
                    f"unauditable",
                )
        reason = entry["reason"].strip()
        date = entry["date"].strip()
        if not reason:
            _fail(i, entry, "empty reason -- an override with no stated reason is not auditable")
        if not date:
            _fail(i, entry, "empty date")

        if player_id in seen:
            # Not an error: a re-judgement is legitimate (PUP in August, activated in September).
            # Logged rather than silent, because two lines for one player in a hand-edited file
            # is also what a botched edit looks like.
            log.info(
                "playing-time entry #%d re-decides %s (previously entry #%d); the later entry "
                "wins",
                i,
                player_id,
                seen[player_id],
            )
        seen[player_id] = i

        out.append(
            PlayingTimeOverride(
                player_id=player_id,
                games=games,
                reason=reason,
                date=date,
                player_name=str(entry.get("player_name") or "").strip(),
                designation=str(entry.get("designation") or "").strip(),
            )
        )
    return tuple(out)


def load_overrides(path: Path | None = None) -> tuple[PlayingTimeOverride, ...]:
    """Read ``data/playing_time.json``. A MISSING file means no overrides.

    A missing file is the ordinary state and must never break a board build. Anything else --
    unreadable, malformed, or present-but-empty -- is NOT ordinary and raises
    :class:`PlayingTimeFileError`. Failing open here would quietly un-apply a human judgement,
    which is the one degradation this module must never do (the same rule, and the same reason,
    as ``decisions.load_decisions``).
    """
    p = Path(path) if path is not None else OVERRIDES_PATH
    if not p.exists():
        return ()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlayingTimeFileError(f"cannot read {p}: {exc}") from exc
    if not text.strip():
        raise PlayingTimeFileError(
            f"{p} exists but is empty. An empty file is not the same as no overrides -- it is "
            "what a truncated write looks like. Delete the file to mean 'no overrides', or "
            "restore its contents."
        )
    try:
        return parse_overrides(text)
    except PlayingTimeFileError as exc:
        raise PlayingTimeFileError(f"{p}: {exc}") from exc


def merge_overrides(
    existing: Iterable[PlayingTimeOverride], incoming: Iterable[PlayingTimeOverride]
) -> tuple[PlayingTimeOverride, ...]:
    """``incoming`` wins on a shared ``player_id``; everything else keeps its original order."""
    by_key: dict[str, PlayingTimeOverride] = {}
    order: list[str] = []
    for o in (*existing, *incoming):
        if o.player_id not in by_key:
            order.append(o.player_id)
        by_key[o.player_id] = o
    return tuple(by_key[k] for k in order)


def save_overrides(overrides: Iterable[PlayingTimeOverride], path: Path | None = None) -> Path:
    """Write the file atomically (temp + ``os.replace``), the way this repo writes any state."""
    p = Path(path) if path is not None else OVERRIDES_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA_VERSION,
        "_note": _FILE_NOTE,
        "overrides": [o.as_json() for o in overrides],
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ------------------------------------------------------------------ the board's input


def overrides_by_pid(
    overrides: Iterable[PlayingTimeOverride],
) -> dict[str, PlayingTimeOverride]:
    """``player_id -> override``, later entries winning. What the board build looks up."""
    out: dict[str, PlayingTimeOverride] = {}
    for o in overrides:
        out[o.player_id] = o
    return out


@dataclass(frozen=True)
class Binding:
    """What an override actually did to one player, once the curve had its say."""

    override: PlayingTimeOverride
    #: The games figure the pipeline would have used with NO override -- always a real number.
    #:
    #: When the active source publishes no games column, that figure is the CURVE, because the
    #: fitted rank-conditional prior is what supplies the volume downstream. Carrying ``None``
    #: here instead (as this did first) made every such override report a change: an override of
    #: 99 on a FantasyPros board clamps to the curve, leaves EVoB byte-identical, and would
    #: still have been badged and pulled out of the injury queue (Codex 2026-08-24 finding 2).
    #: Whether a source published a figure at all is a separate question --
    #: :attr:`source_published_games` answers it, for the tooltip.
    was: float
    #: The games figure in force now: ``min(override.games, curve)``.
    now: float
    #: The curve value that clamped it, carried so a badge can explain an upward clamp.
    curve: float
    #: Did the active source publish a games figure for this player at all? ``False`` means
    #: ``was`` came from the fitted prior rather than from a source. Display only -- it never
    #: affects :attr:`moved`, which is a pure comparison of numbers.
    source_published_games: bool = True

    @property
    def clamped(self) -> bool:
        """Did the availability curve cut the override down?"""
        return self.override.games > self.curve + 1e-9

    @property
    def moved(self) -> bool:
        """Did this override actually CHANGE the number the board uses?

        A pure numeric comparison against the no-override counterfactual. An override that lands
        on the figure the board already had moved nothing, however it got there -- clamped down
        from an absurd number, or simply restating the prior.
        """
        return abs(self.now - self.was) > 1e-9

    def describe(self) -> str:
        before = (
            f"{self.was:.2f}"
            if self.source_published_games
            else f"{self.was:.2f} (fitted prior -- this source publishes no games)"
        )
        tail = (
            f" (clamped from {self.override.games:.2f} by the {self.curve:.2f} healthy-rank "
            f"curve)"
            if self.clamped
            else ""
        )
        return f"{self.override.describe()} -- games {before} -> {self.now:.2f}{tail}"


def bind(override: PlayingTimeOverride, *, source_games: float | None, curve: float) -> Binding:
    """Apply one override under the documented semantics: ``min(override.games, curve)``.

    Args:
        override: the human judgement.
        source_games: the games figure the active source published, ALREADY capped by ``curve``,
            or ``None`` when the source publishes none (FantasyPros, FantasySharks). Two traps
            live in this one argument, both hit for real:

            * It must be the CAPPED figure, not the raw source number. Otherwise
              :attr:`Binding.moved` reports a change for every override on a player the curve had
              already cut down (Josh Allen: source 17.0, curve 16.6).
            * ``None`` is resolved to ``curve`` here, not carried through as "unknown". When a
              source publishes no games, the fitted prior supplies the volume downstream, so the
              curve IS the no-override figure -- treating ``None`` as unconditionally "moved"
              badged overrides that changed nothing (Codex 2026-08-24 finding 2).
        curve: the rank-conditional availability figure for this player's position and PPG rank.
            The ceiling, per the module docstring -- an override may lower a player freely and
            may restore him only as far as this.

    Returns:
        A :class:`Binding` recording the before, the after, and whether anything moved.
    """
    published = source_games is not None
    return Binding(
        override=override,
        was=float(source_games) if published else float(curve),
        now=min(float(override.games), float(curve)),
        curve=float(curve),
        source_published_games=published,
    )


def new_override(
    *,
    player_id: str,
    games: float,
    reason: str,
    player_name: str = "",
    designation: str = "",
    date: str | None = None,
) -> PlayingTimeOverride:
    """An override stamped with today's date unless one is supplied."""
    return PlayingTimeOverride(
        player_id=str(player_id),
        games=float(games),
        reason=reason,
        date=date or _date.today().isoformat(),
        player_name=player_name,
        designation=designation,
    )
