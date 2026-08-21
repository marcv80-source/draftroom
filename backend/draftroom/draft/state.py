"""Live draft state, derived purely by replaying the event log.

There is deliberately no mutable state here that isn't reconstructible from (snapshot, events).
`DraftState.replay(...)` is the only way to build one. That is what makes crash recovery a five-second
relaunch instead of a judgment call about what was and wasn't saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from draftroom.draft import snake
from draftroom.draft.events import Event, EventLog


@dataclass
class Pick:
    pick_no: int
    team_slot: int
    player_id: str | None = None
    # A stub is a player who was drafted but isn't in our database -- a deep rookie, a name nobody
    # recognises. Two keystrokes in the UI. Recorded so the board stays complete and the opponent
    # model still sees that a roster spot at that position was consumed.
    stub_name: str | None = None
    stub_pos: str | None = None
    voided: bool = False
    out_of_order: bool = False

    @property
    def is_filled(self) -> bool:
        return not self.voided and (self.player_id is not None or self.stub_name is not None)

    @property
    def label_id(self) -> str | None:
        return self.player_id or (f"stub:{self.stub_name}" if self.stub_name else None)


@dataclass
class DraftState:
    teams: int
    rounds: int
    my_slot: int
    picks: dict[int, Pick] = field(default_factory=dict)
    current_pick: int = 1
    # Draft slot -> display name, for slots that have one set. Slots absent from this dict fall
    # back to the "Team N" (or "YOU") default -- see `team_label` below. Populated purely by
    # replaying `team_named` events (plan A1); an empty-string name clears a slot's entry.
    team_names: dict[int, str] = field(default_factory=dict)

    # ---------- derived views ----------

    def team_label(self, slot: int) -> str:
        """Name-aware team label, in precedence order: (1) a name set for this slot, (2) "YOU"
        for `my_slot`, (3) "Team {slot}". Every caller that renders a team by slot number
        (upcoming picks, opponent grid, the draft-results tab) must go through this -- a stale
        "Team N" anywhere the name is actually known is a bug (plan A1)."""
        name = self.team_names.get(slot)
        if name:
            return name
        if slot == self.my_slot:
            return "YOU"
        return f"Team {slot}"

    @property
    def drafted_player_ids(self) -> set[str]:
        return {
            p.player_id
            for p in self.picks.values()
            if p.is_filled and p.player_id is not None
        }

    def roster(self, team_slot: int) -> list[Pick]:
        return [
            p
            for p in sorted(self.picks.values(), key=lambda x: x.pick_no)
            if p.team_slot == team_slot and p.is_filled
        ]

    def roster_positions(self, team_slot: int, pos_of: dict[str, str]) -> dict[str, int]:
        """Count of filled roster spots by position for one team.

        `pos_of` maps player_id -> position; stubs carry their own position. This is the input to
        opponent-need modelling, which is why stubs must be positioned rather than discarded.
        """
        counts: dict[str, int] = {}
        for p in self.roster(team_slot):
            pos = p.stub_pos if p.player_id is None else pos_of.get(p.player_id)
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        return counts

    def unfilled_starters(
        self, team_slot: int, starters: dict[str, int], pos_of: dict[str, str]
    ) -> dict[str, int]:
        have = self.roster_positions(team_slot, pos_of)
        return {
            pos: max(0, need - have.get(pos, 0))
            for pos, need in starters.items()
            if max(0, need - have.get(pos, 0)) > 0
        }

    @property
    def slot_on_clock(self) -> int:
        return snake.slot_on_clock(self.teams, self.current_pick)

    @property
    def is_my_pick(self) -> bool:
        return self.slot_on_clock == self.my_slot

    def turn_context(self) -> snake.TurnContext:
        return snake.TurnContext.build(
            self.teams, self.my_slot, self.rounds, self.current_pick
        )

    def picks_remaining_for(self, team_slot: int) -> int:
        total = self.rounds
        used = len([p for p in self.picks.values() if p.team_slot == team_slot and p.is_filled])
        return max(0, total - used)

    def gaps(self) -> list[int]:
        """Pick numbers before the clock that were never filled.

        These are the picks Marc missed while talking to someone. The UI flags them so an incomplete
        board is visible rather than silently wrong -- a missing pick would otherwise make a drafted
        player look available.
        """
        return [
            n
            for n in range(1, self.current_pick)
            if n not in self.picks or not self.picks[n].is_filled
        ]

    # ---------- replay ----------

    @classmethod
    def replay(
        cls, events: Iterable[Event], *, teams: int, rounds: int, my_slot: int
    ) -> "DraftState":
        st = cls(teams=teams, rounds=rounds, my_slot=my_slot)
        # Undo is resolved during replay rather than by rewriting history: an `undo` event names the
        # sequence number it cancels, and we drop that event on the way through.
        evs = list(events)
        undone: set[int] = set()
        for ev in evs:
            if ev.type == "undo":
                target = ev.payload.get("undo_seq")
                if target is not None:
                    undone.add(int(target))
        for ev in evs:
            if ev.seq in undone or ev.type == "undo":
                continue
            st._apply(ev)
        return st

    def _apply(self, ev: Event) -> None:
        p = ev.payload
        if ev.type == "draft_started":
            self.current_pick = int(p.get("current_pick", 1))

        elif ev.type in ("pick", "stub_created"):
            pick_no = int(p["pick_no"])
            team_slot = int(p.get("team_slot") or snake.slot_on_clock(self.teams, pick_no))
            # out_of_order means "this pick did not go to the team on the clock". It is
            # recomputed here rather than trusted from the payload: click-anywhere drafting
            # (plan A2) ALWAYS supplies a team_slot -- the menu defaults to whoever is on the
            # clock -- so the old `out_of_order=team_slot is not None` marked every ordinary
            # pick OOO (Codex 2026-08-21 finding 7). Recomputing also keeps old logs correct,
            # because the payload flag stays in history but no longer drives the display.
            self.picks[pick_no] = Pick(
                pick_no=pick_no,
                team_slot=team_slot,
                player_id=p.get("player_id"),
                stub_name=p.get("stub_name"),
                stub_pos=p.get("stub_pos"),
                out_of_order=team_slot != snake.slot_on_clock(self.teams, pick_no),
            )
            # Backfilling a missed pick must not drag the clock backwards.
            if pick_no >= self.current_pick:
                self.current_pick = pick_no + 1

        elif ev.type == "pick_corrected":
            pick_no = int(p["pick_no"])
            existing = self.picks.get(pick_no)
            if existing is None:
                return
            existing.player_id = p.get("player_id")
            existing.stub_name = p.get("stub_name")
            existing.stub_pos = p.get("stub_pos")
            existing.voided = False
            # Reassign-to-team (plan A3): a correction only moves team_slot when the event
            # explicitly carries one, so a correction that never mentioned team_slot leaves
            # ownership byte-for-byte unchanged, matching every correction made before this
            # feature existed.
            if p.get("team_slot") is not None:
                existing.team_slot = int(p["team_slot"])

        elif ev.type == "pick_reassigned":
            # Ownership only. Identity, void state and out_of_order metadata are untouched --
            # that separation is the whole reason this is not a `pick_corrected` (Codex
            # 2026-08-21 finding 3). out_of_order is recomputed because "was this pick made by
            # someone other than the team on the clock" is a fact about the new owner.
            pick_no = int(p["pick_no"])
            existing = self.picks.get(pick_no)
            if existing is None:
                return
            existing.team_slot = int(p["team_slot"])
            existing.out_of_order = existing.team_slot != snake.slot_on_clock(
                self.teams, pick_no
            )

        elif ev.type == "pick_voided":
            pick_no = int(p["pick_no"])
            if pick_no in self.picks:
                self.picks[pick_no].voided = True

        elif ev.type == "clock_set":
            self.current_pick = int(p["current_pick"])

        elif ev.type == "team_named":
            slot = int(p["team_slot"])
            name = str(p.get("name", "")).strip()
            if name:
                self.team_names[slot] = name
            else:
                # An empty name clears back to the "Team N" default rather than storing "".
                self.team_names.pop(slot, None)


class DraftSession:
    """Thin command layer over the log. Every mutation is an appended event, then a replay."""

    def __init__(self, log: EventLog, *, teams: int, rounds: int, my_slot: int):
        self.log = log
        self.teams = teams
        self.rounds = rounds
        self.my_slot = my_slot
        self.state = DraftState.replay(
            log.events(), teams=teams, rounds=rounds, my_slot=my_slot
        )

    def _refresh(self) -> DraftState:
        self.state = DraftState.replay(
            self.log.events(), teams=self.teams, rounds=self.rounds, my_slot=self.my_slot
        )
        return self.state

    def record_pick(
        self,
        player_id: str,
        *,
        pick_no: int | None = None,
        team_slot: int | None = None,
        raw_query: str = "",
    ) -> DraftState:
        """The one-keystroke path: assign to whoever is on the clock and advance."""
        n = pick_no if pick_no is not None else self.state.current_pick
        # No out_of_order in the payload: it is DERIVED at replay from (team_slot, pick_no)
        # so the log cannot carry a flag that contradicts its own numbers.
        self.log.append(
            "pick",
            pick_no=n,
            team_slot=team_slot,
            player_id=player_id,
            raw_query=raw_query,
        )
        return self._refresh()

    def record_stub(
        self, name: str, pos: str, *, pick_no: int | None = None, team_slot: int | None = None
    ) -> DraftState:
        n = pick_no if pick_no is not None else self.state.current_pick
        self.log.append(
            "stub_created",
            pick_no=n,
            team_slot=team_slot,
            stub_name=name,
            stub_pos=pos,
        )
        return self._refresh()

    def undo_last(self) -> DraftState:
        """Cancel the most recent still-live pick-ish event."""
        live = [
            e
            for e in self.log.events()
            if e.type in ("pick", "stub_created", "pick_corrected", "pick_voided", "clock_set")
        ]
        already = {
            int(e.payload["undo_seq"])
            for e in self.log.events()
            if e.type == "undo" and "undo_seq" in e.payload
        }
        for ev in reversed(live):
            if ev.seq not in already:
                self.log.append("undo", undo_seq=ev.seq)
                break
        return self._refresh()

    def correct_pick(
        self, pick_no: int, *, player_id: str | None = None, stub_name: str | None = None,
        stub_pos: str | None = None, team_slot: int | None = None,
    ) -> DraftState:
        self.log.append(
            "pick_corrected",
            pick_no=pick_no,
            player_id=player_id,
            stub_name=stub_name,
            stub_pos=stub_pos,
            team_slot=team_slot,
        )
        return self._refresh()

    def void_pick(self, pick_no: int) -> DraftState:
        self.log.append("pick_voided", pick_no=pick_no)
        return self._refresh()

    def reassign_pick(self, pick_no: int, team_slot: int) -> DraftState:
        """Move ONE pick to a different draft slot, changing nothing else about it."""
        self.log.append("pick_reassigned", pick_no=pick_no, team_slot=team_slot)
        return self._refresh()

    def _last_live_pick_event(self) -> Event | None:
        """The most recent pick-ish event that has not itself been undone."""
        already = {
            int(e.payload["undo_seq"])
            for e in self.log.events()
            if e.type == "undo" and "undo_seq" in e.payload
        }
        live = [
            e
            for e in self.log.events()
            if e.type in ("pick", "stub_created", "pick_corrected", "pick_voided", "clock_set")
            and e.seq not in already
        ]
        return live[-1] if live else None

    def undraft_pick(self, pick_no: int) -> tuple[DraftState, str]:
        """Remove one pick with ONE event, and rewind the clock when that is the honest thing.

        This exists because "undraft" is two genuinely different acts wearing one button, and the
        UI previously did the wrong one for the common case (Codex 2026-08-21 finding 2):

        * The pick being removed is the newest one -- the ordinary "wrong name, take it back"
          case. Appending `pick_voided` marked it void but left `current_pick` advanced, so the
          replacement player landed at the NEXT pick number for the NEXT team, and every
          subsequent pick on the physical board was attributed one slot off. The right act is an
          `undo`, which drops the pick event during replay so the clock returns on its own.
        * The pick being removed is older. History cannot rewind without renumbering everything
          after it, so it becomes a `pick_voided` and leaves a hole that `gaps()` reports and the
          UI shows, which is exactly what an unfilled sticker on the board looks like.

        Returns (state, mode) where mode is "undone" or "voided", so the caller can say which
        happened rather than leaving Marc to infer it from the clock.

        Deliberately ONE appended event either way. A void+clock_set pair would let a crash land
        between the two halves and leave the log describing a draft that never happened.
        """
        last = self._last_live_pick_event()
        is_newest_event = (
            last is not None
            and last.type in ("pick", "stub_created")
            and int(last.payload.get("pick_no", -1)) == pick_no
        )
        if is_newest_event:
            self.log.append("undo", undo_seq=last.seq)
            return self._refresh(), "undone"
        self.log.append("pick_voided", pick_no=pick_no)
        return self._refresh(), "voided"

    def set_clock(self, pick_no: int) -> DraftState:
        self.log.append("clock_set", current_pick=pick_no)
        return self._refresh()

    def set_team_name(self, team_slot: int, name: str) -> DraftState:
        """Set (or, with an empty `name`, clear) the display name for one draft slot."""
        self.log.append("team_named", team_slot=team_slot, name=name)
        return self._refresh()
