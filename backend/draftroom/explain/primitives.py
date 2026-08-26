"""The explanation contract.

This is the part of the tool Marc actually asked for. The model's job is not to hand down a pick, it
is to tell him what it sees and let him decide, because he is in the room and knows things the model
cannot: that someone just traded away their season, that the guy picking ahead of him is a Bengals fan,
that a player looked hurt in preseason.

Two rules encoded here rather than left to whoever writes the UI:

1. **Every primitive carries the number it is claiming.** No bullet may say "he probably won't last"
   when it could say "18% chance he's there at 2.04". A recommendation you can't audit mid-draft is a
   recommendation you stop trusting the first time it's wrong.

2. **Every recommendation carries its fallbacks.** The engine informs, it never insists. If Marc
   doesn't like the top name, the next two are already on screen with what they cost him.

The UI renders 2-3 of these as scannable bullets, chosen by salience. It never renders all of them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class Urgency(str, Enum):
    """How hard this consideration is pulling. Drives ordering and colour, nothing else."""

    CRITICAL = "critical"  # act now or lose the option entirely
    NOTABLE = "notable"
    CONTEXT = "context"


@dataclass(frozen=True)
class SurvivalInfo:
    """Odds this player is still on the board when Marc picks again."""

    next_pick: int
    next_pick_label: str  # "2.04" -- the draft-board label, which is how the room talks
    p_survive_next: float
    following_pick: int | None = None
    following_pick_label: str | None = None
    p_survive_following: float | None = None

    @property
    def is_coin_flip(self) -> bool:
        return 0.35 <= self.p_survive_next <= 0.65

    @property
    def is_gone(self) -> bool:
        return self.p_survive_next < 0.10


@dataclass(frozen=True)
class TierCliff:
    """The cost of missing this tier.

    `points_to_next_tier` is the honest version of "he's a tier above" -- a tier boundary is only
    worth acting on if the drop across it is big, and sometimes it isn't.
    """

    tier_index: int
    tier_size_remaining: int
    points_to_next_tier: float
    exhaustion_pick: int | None  # first pick where this tier is expected to be empty
    exhaustion_label: str | None = None


@dataclass(frozen=True)
class OpponentPressure:
    """Who picks between now and Marc's next turn, and what they still need.

    This is the piece no cheat sheet can give him, and in a two-QB league it is usually the single
    most predictive signal on the board: teams with an empty QB slot take quarterbacks.
    """

    position: str
    teams_before_next_turn: int
    teams_needing_position: int
    manager_names: tuple[str, ...] = ()
    league_timing_offset: float | None = None  # picks earlier than national ADP, from last year
    run_detected: bool = False


@dataclass(frozen=True)
class PositionDepth:
    """How much cushion is left at a position. The answer to 'can I wait?'"""

    position: str
    startable_remaining: int
    league_demand_remaining: int  # starter slots still unfilled across the whole league
    picks_of_cushion: float | None = None

    @property
    def is_shutout_risk(self) -> bool:
        """More unfilled starter slots in the league than startable players to fill them."""
        return self.startable_remaining < self.league_demand_remaining


@dataclass(frozen=True)
class Fallback:
    """If not him, then who, and what does it cost."""

    player_id: str
    name: str
    pos: str
    points_behind: float
    p_survive_next: float


@dataclass(frozen=True)
class Counterfactual:
    """What taking this player costs at the next pick.

    Stated as a trade rather than a score, because that is how the decision actually feels:
    'taking the QB now costs you about 6 points of running back at your next pick.'
    """

    position_given_up: str
    points_given_up: float
    position_gained: str
    points_gained: float

    @property
    def net(self) -> float:
        return self.points_gained - self.points_given_up


@dataclass
class Candidate:
    """One recommendation, with everything needed to explain and audit it."""

    player_id: str
    name: str
    pos: str
    team: str
    bye: int | None

    draft_value: float
    projected_points: float
    floor: float
    ceiling: float
    utility: float  # the ranking key: E[value] - lambda * sd

    tier: TierCliff
    survival: SurvivalInfo
    depth: PositionDepth
    vona: float
    opponent_pressure: OpponentPressure | None = None
    counterfactual: Counterfactual | None = None
    fallbacks: tuple[Fallback, ...] = ()
    flags: tuple[str, ...] = ()
    #: The engine's OWN gate level for this candidate, and the primary key it sorts by before
    #: `utility`: 2 = a scarcity-floor position, 1 = the opportunistic elite-QB grab, 0 = ranked
    #: on value alone. Published (ledger #12) so the ALL board can reuse the panel's ranking for
    #: the players the panel actually ranks, instead of reconstructing it from the parts and
    #: hoping the reconstruction agrees. It cannot: at the turn the panel optimises a PAIR, and
    #: mid-round `utility` carries a candidate-specific continuation and risk term, so
    #: `value + VONA` matches its ORDERING only up to a position-agnostic constant (Codex
    #: 2026-08-26 -- the earlier 16-of-16 agreement was one board state, not an identity).
    gate_priority: int = 0

    # Set by the renderer; kept on the object so the UI and any log see identical text.
    bullets: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Recommendation:
    """The full response for one pick: ranked candidates plus anything board-wide worth shouting."""

    pick_no: int
    pick_label: str
    on_the_clock: int
    is_my_pick: bool
    candidates: tuple[Candidate, ...]
    warnings: tuple[str, ...] = ()  # shut-out risk, bye collisions, positional runs
    at_the_turn: bool = False
    picks_until_next: int | None = None
    #: VONA per POSITION -- the points given up by waiting one turn at that position. Published
    #: board-wide (ledger #12) because it is the term that turns a season-value ranking into a
    #: pick-now ranking, and the ALL board needs it for every player, not just the candidates.
    #: Empty when there is no following turn to wait for (the final round).
    vona_by_pos: Mapping[str, float] = field(default_factory=dict)
    #: Positions the engine RANKED FIRST regardless of value, from the deterministic scarcity
    #: floor. A hard gate, not a price.
    forced_positions: tuple[str, ...] = ()
    #: Players the opportunistic elite-QB grab ranked first regardless of value. The OTHER hard
    #: gate, and the one that actually fires early: at pick 1.01 of the 2026 board this is the
    #: top-3 board QBs, which is why the panel leads with Allen/Lamar/Maye while the pick-now
    #: ordering puts Allen eighth. Both gates are published so the board can BADGE what the
    #: panel is elevating instead of silently disagreeing with it.
    elite_player_ids: tuple[str, ...] = ()
