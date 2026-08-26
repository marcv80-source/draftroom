// Mirrors the JSON shapes produced by backend/draftroom/server.py.
//
// The Recommendation/Candidate shape mirrors backend/draftroom/explain/primitives.py exactly
// (that module is the explicit contract for what the UI renders -- see its docstring). Fields
// there are frozen dataclasses turned into plain dicts by `dataclasses.asdict`, so every
// nested object below is optional exactly where the Python side allows `None`.

export interface TierCliff {
  tier_index: number;
  tier_size_remaining: number;
  points_to_next_tier: number;
  exhaustion_pick: number | null;
  exhaustion_label: string | null;
}

export interface SurvivalInfo {
  next_pick: number;
  next_pick_label: string;
  p_survive_next: number;
  following_pick: number | null;
  following_pick_label: string | null;
  p_survive_following: number | null;
}

export interface PositionDepth {
  position: string;
  startable_remaining: number;
  league_demand_remaining: number;
  picks_of_cushion: number | null;
}

export interface DemandClockEntry {
  position: string;
  startable_remaining: number;
  league_demand_remaining: number;
  teams_needing_before_next_turn: number;
  picks_before_next_turn: number;
  cushion: number;
}

export interface OpponentPressure {
  position: string;
  teams_before_next_turn: number;
  teams_needing_position: number;
  manager_names: string[];
  league_timing_offset: number | null;
  run_detected: boolean;
}

export interface Counterfactual {
  position_given_up: string;
  points_given_up: number;
  position_gained: string;
  points_gained: number;
}

export interface Fallback {
  player_id: string;
  name: string;
  pos: string;
  points_behind: number;
  p_survive_next: number;
}

export interface Candidate {
  player_id: string;
  name: string;
  pos: string;
  team: string;
  bye: number | null;
  draft_value: number;
  projected_points: number;
  floor: number;
  ceiling: number;
  utility: number;
  tier: TierCliff;
  survival: SurvivalInfo;
  depth: PositionDepth;
  vona: number;
  opponent_pressure: OpponentPressure | null;
  counterfactual: Counterfactual | null;
  fallbacks: Fallback[];
  flags: string[];
  bullets: string[];
  // Ledger #12: the engine's own gate level, and the key it sorts by BEFORE utility. 2 = scarcity
  // floor, 1 = elite-QB grab, 0 = ranked on value alone. This is the authoritative gate for the
  // ALL board -- it has already been through feasibility and the per-position top-N cut, which is
  // what makes it safe to hoist while `forced_positions` is not.
  gate_priority?: number;
}

export interface Recommendation {
  pick_no: number;
  pick_label: string;
  on_the_clock: number;
  is_my_pick: boolean;
  candidates: Candidate[];
  warnings: string[];
  at_the_turn: boolean;
  picks_until_next: number | null;
  // Ledger #6. Non-null = this answer is for a pick that is NOT on the clock, i.e. a preview of
  // Marc's own next turn computed against who is available RIGHT NOW. It does not simulate the
  // intervening picks being taken, so the UI must label it rather than present it as live.
  preview_for_pick?: number | null;
  // How many picks away that preview is. 0 when this is the live on-the-clock answer.
  picks_away?: number;
  // Ledger #12. VONA per POSITION -- the points given up by waiting one turn at that position.
  // This is the term that turns a season-value board into a pick-now board, and the ALL view
  // sorts on `value + vona_by_pos[pos]`. Absent/empty in the final round (nothing to wait for)
  // and in placeholder mode, where the ALL view falls back to plain draft value.
  vona_by_pos?: Record<string, number>;
  // Positions the deterministic scarcity floor fired on. EXPLANATORY ONLY -- never a sort key.
  // A floor applies to a whole position, so using it to gate the board hoisted every remaining
  // player there (QB23 and below included) above every other position.
  forced_positions?: string[];
  // Players the opportunistic elite-QB grab covers. Also explanatory: the authoritative gate is
  // per-candidate `gate_priority`, which has already been through feasibility and the
  // per-position top-N cut.
  elite_player_ids?: string[];
}

export interface UpcomingPick {
  pick_no: number;
  pick_label: string;
  team_slot: number;
  team_label: string;
  is_mine: boolean;
  is_on_clock: boolean;
  filled: boolean;
}

export interface RosterEntry {
  pick_no: number;
  pick_label: string;
  team_slot: number;
  player_id: string | null;
  name: string | null;
  pos: string | null;
  team: string | null;
  is_stub: boolean;
  voided: boolean;
  out_of_order: boolean;
}

export interface StarterFill {
  starters: Record<string, { filled: number; need: number }>;
  flex: { filled: number; need: number };
  bench_used: number;
  bench_size: number;
}

export interface OpponentTeam {
  team_slot: number;
  team_label: string;
  is_mine: boolean;
  counts: Record<string, number>;
  qb_count: number;
  qb_unfilled: number;
  unfilled: Record<string, number>;
  starter_fill: StarterFill;
  qb_complete: boolean;
  roster: RosterEntry[];
  open_slots_summary: string;
}

export interface TierRow {
  player_id: string;
  name: string;
  team: string;
  bye: number | null;
  adp: number;
  value: number;
  is_ranked: boolean;
  // False on a RANKED row = the real board excluded this player (name kept for bookkeeping,
  // the value carries no evaluation). Also false everywhere in placeholder fallback mode.
  value_is_real: boolean;
  drafted: boolean;
  owner_team_slot: number | null;
  owner_label: string | null;
  tier: number | null;
  sigma_ppg: number | null;
  disagreement_high: boolean;
  injury_status: string | null;
  // Plan A5: per-source value for this player so a row can show every source side by side
  // without a refetch. Optional/nullable because a backend not yet on the multi-source
  // composite (or a source lacking this player) omits it -- treat missing as "not available",
  // never as agreement.
  value_by_source?: Record<string, number> | null;
  // A rejection Marc adjudicated in the prep-time review queue that actually changed this
  // player's value (docs/REVIEW_QUEUE.md). Absent/null = nothing was rejected for him. This is
  // rendered as a visible badge on purpose: a decision of his is never silently folded into a
  // number, so the board can always explain why it disagrees with the raw sources.
  projection_decisions?: AppliedDecision[] | null;
  // Marc's manual playing-time override, when one actually MOVED this player's expected games
  // (backend: draftroom.valuation.playing_time). Absent/null = no override changed anything for
  // him. Badged for the same reason a rejection is: an expected-games figure that came from a
  // human must never read as a model output.
  playing_time?: AppliedPlayingTime | null;
}

export interface AppliedDecision {
  source: string;
  stat: string;
  verdict: "reject";
  reason: string;
  date: string;
  detector: string;
}

export interface AppliedPlayingTime {
  // The figure in force on the board: min(requested_games, curve).
  games: number;
  // What Marc actually wrote. Differs from `games` only when `clamped` is true.
  requested_games: number;
  // What the pipeline would have used with no override. Always a real number: for a source with
  // no games column it is the fitted prior's own figure, because that is what the board would
  // have used. `source_published_games` says which of the two it came from.
  was: number;
  // False = the active source publishes no games column, so `was` is the fitted prior.
  source_published_games: boolean;
  // The healthy-rank availability curve figure, which is the ceiling an override cannot exceed.
  curve: number;
  // True when the curve cut the override down -- the one case where the board's number is NOT
  // the number Marc wrote, so the badge has to say so.
  clamped: boolean;
  reason: string;
  date: string;
  designation: string;
}

// Plan A3 -- the draft in pick order, INCLUDING voided picks (the results tab is the audit
// trail; hiding voided rows would defeat the append-only design). Sorted by pick_no.
export interface DraftPick {
  pick_no: number;
  pick_label: string; // e.g. "3.07"
  round: number; // 1-based
  team_slot: number;
  team_label: string; // name-aware, see A1's team_label()
  is_mine: boolean;
  player_id: string | null;
  name: string | null;
  pos: string | null;
  team: string | null;
  bye: number | null;
  is_stub: boolean;
  voided: boolean;
  out_of_order: boolean;
}

// Plan A5/B2 -- GET /api/sources.
export interface SourceInfo {
  key: string;
  label: string;
  player_count: number;
  // The server computes this (a source whose board built but valued nobody is NOT available)
  // and the type used to drop it on the floor, so every source rendered as selectable --
  // including ones that would serve ADP placeholders (Codex 2026-08-21 finding 5).
  available: boolean;
  note: string;
}

export interface SourcesResponse {
  active: string;
  sources: SourceInfo[];
}

export interface DraftState {
  teams: number;
  rounds: number;
  my_slot: number;
  current_pick: number;
  current_pick_label: string;
  slot_on_clock: number;
  is_my_pick: boolean;
  next_pick: number | null;
  gap_to_next: number | null;
  at_the_turn: boolean;
  gaps: number[];
  upcoming_picks: UpcomingPick[];
  my_roster: RosterEntry[];
  my_starter_fill: StarterFill;
  opponents: OpponentTeam[];
  tier_board: Record<string, TierRow[]>;
  demand_clock: Record<string, DemandClockEntry>;
  elite_qb_rank_cutoff_default: number;
  // Monotone event counter (bumps on pick/stub/correct/void/clock/undo). Key recommendation
  // refetches on THIS, not current_pick -- void/correct change availability without moving
  // the clock.
  event_seq: number;
  // "real" = values are the validated board; "placeholder" = ADP fallback, not trustworthy
  // for recommendations (surfaced as a banner).
  board_source: "real" | "placeholder";
  real_value_count: number;
  value_note: string;
  // Plan A1 -- slots with a name explicitly set (via team_named events). Keys are team_slot as
  // a STRING because JSON object keys always are. Slots absent here fall back to "YOU"/"Team N"
  // -- read team_label off upcoming_picks/opponents/all_picks for display, this map is only for
  // pre-filling the edit form.
  team_names: Record<string, string>;
  // Plan A3 -- the draft in pick order, including voided picks. Optional/possibly-undefined
  // until the backend half of A3 ships; treat absence as "results tab has nothing to show yet",
  // never as "the draft is empty".
  all_picks?: DraftPick[];
  // Plan B2 -- the projection source the SERVER is actually serving. The server has always sent
  // this; the type omitted it, so SourceToggle kept its own copy and the header could name a
  // source the server had since moved off (Codex 2026-08-21 finding 6). Server state wins.
  active_source?: string;
  // Plan A1 -- the ten real league names from data/league_manual.yaml. The naming panel must
  // read these rather than carry its own hardcoded copy (Codex 2026-08-21 finding 10).
  team_name_candidates?: string[];
  // Set by POST /api/undraft so the UI can say which of the two things happened: "undone" also
  // rewound the clock, "voided" left a gap that `gaps` now reports.
  last_undraft?: { pick_no: number; mode: "undone" | "voided" };
}

export interface SearchMatch {
  player_id: string;
  name: string;
  pos: string;
  team: string;
  overall_rank: number;
  score: number;
  reason: string;
  drafted: boolean;
  is_ranked: boolean;
}

export interface SearchResponse {
  query: string;
  matches: SearchMatch[];
}

export const POSITIONS = ["QB", "RB", "WR", "TE"] as const;
export type Position = (typeof POSITIONS)[number];

// Ledger #2: the cross-position best-available view. NOT a position, deliberately -- it is a
// board FILTER, and keeping it out of `Position` means nothing that means "a real position"
// (the server payload, the demand clock, stub creation) can accidentally receive it.
// Marc: "especially early in the draft as we're thinking about best available and I'm not
// focused on a position... at all times it might be worth the risk to take someone who is of
// exceptional value."
export const ALL_FILTER = "ALL" as const;
export type BoardFilter = Position | typeof ALL_FILTER;
export const BOARD_FILTERS = [ALL_FILTER, ...POSITIONS] as const;
