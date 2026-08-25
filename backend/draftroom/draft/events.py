"""Append-only draft event log.

The whole crash-safety story lives here. Draft state is never stored as mutable rows that could be
half-written when a laptop dies mid-round. It is a pure function of (immutable snapshot, ordered events),
so recovery is just replay. Every event is one JSON line, flushed and fsync'd BEFORE the UI acknowledges
the pick -- if Marc sees the pick land on screen, it survived to disk.

Corrections never destroy history: a mistake is a new event that supersedes an earlier one, so the board
can always be reconstructed and audited after the fact.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

EventType = Literal[
    "draft_started",
    "pick",
    "pick_corrected",
    "pick_voided",
    "stub_created",
    "clock_set",
    "undo",
    # Team display name, keyed by draft slot. Payload: {"team_slot": int, "name": str}. Last
    # event wins on replay; an empty-string name clears the name back to the "Team N" default
    # (plan A1, docs/archive/PLAN_2026-08-20.md).
    "team_named",
    # Moves ONE pick's ownership to a different draft slot, changing nothing else. Payload:
    # {"pick_no": int, "team_slot": int}. Deliberately separate from `pick_corrected`: a
    # correction carries player identity, so reusing it to move ownership meant the frontend had
    # to resend player_id (and a reassign-only request, which sends none, was rejected 422 --
    # Codex 2026-08-21 finding 3). Replay changes team_slot and NOTHING else, so identity, void
    # state and correction history all survive a reassign.
    "pick_reassigned",
    # Records which projection source/board was active when picks around it were made.
    # Payload: {"key": str}. Purely an audit-trail entry -- see DraftBoard.switch_source in
    # server.py, which is the actual source of truth for the currently-served pool (plan B2).
    "source_changed",
    # WHICH DRAFT SLOT IS MARC'S. Payload: {"my_slot": int}. Last event wins on replay.
    #
    # Added 2026-08-25 for feedback ledger #4. The slot is drawn at the table, minutes before the
    # draft, and until this event it could only be supplied at launch (--my-slot / draft_slot in
    # the yaml) -- so the only way to correct it was to restart the server mid-draft. That is not
    # a cosmetic gap: EVERY turn-dependent number is derived from my_slot (survival to the next
    # turn, gap_to_next, the demand-clock window, upcoming picks, the whole recommendation), so a
    # wrong slot makes all of them confidently wrong at once.
    #
    # It is an EVENT rather than a mutable field for the same reason every other correction is:
    # a relaunch has to come back on the slot he set, not the one the command line happened to
    # carry. Marc's words: "I need to be able to set the order and make it clear which one is me."
    "my_slot_set",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    seq: int
    type: EventType
    t: str = field(default_factory=_utc_now)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def from_json(line: str) -> "Event":
        raw = json.loads(line)
        return Event(seq=raw["seq"], type=raw["type"], t=raw["t"], payload=raw.get("payload", {}))


class EventLog:
    """Durable append-only JSONL log.

    Durability contract: `append` does not return until the bytes are on the platter. That costs a
    millisecond or two per pick, which is invisible next to a human typing a name, and it is the
    difference between losing six rounds and losing nothing.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        if not self.path.exists():
            return 0
        last = 0
        for ev in self.read():
            last = max(last, ev.seq)
        return last

    @property
    def last_seq(self) -> int:
        """The most recent event's sequence number (0 for an empty log). Monotone across every
        append, so it doubles as a cheap state-version counter for clients deciding whether
        anything changed (the UI keys recommendation refetches on it)."""
        return self._seq

    def append(self, type_: EventType, **payload: Any) -> Event:
        self._seq += 1
        ev = Event(seq=self._seq, type=type_, payload=payload)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(ev.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return ev

    def read(self) -> Iterator[Event]:
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield Event.from_json(line)
                except (json.JSONDecodeError, KeyError) as exc:
                    # A torn final line is the expected shape of a hard crash: the process died
                    # mid-write. Every earlier line is intact, so we surface it and keep going
                    # rather than refusing to load a draft that is 99% recoverable.
                    raise CorruptEventLog(
                        f"{self.path}:{lineno} is unreadable ({exc}). "
                        "Every preceding event is intact -- delete the trailing line to recover."
                    ) from exc

    def events(self) -> list[Event]:
        return list(self.read())


class CorruptEventLog(RuntimeError):
    pass
