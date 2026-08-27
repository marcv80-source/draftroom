import { useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  ALL_FILTER,
  BOARD_FILTERS,
  type AppliedDecision,
  type AppliedPlayingTime,
  type BoardFilter,
  type ResearchNote,
  type TierRow,
} from "../types";

// Injury statuses worth calling out on the board. Sleeper's field is free-text; these are the
// ones that actually change a draft decision. Anything else (e.g. a plain "Active") is ignored.
const INJURY_BADGE_LABELS: Record<string, string> = {
  Out: "O",
  IR: "IR",
  PUP: "PUP",
  Questionable: "Q",
  Doubtful: "D",
  Suspended: "SUSP",
  NA: "NA",
};

function InjuryBadge({ status }: { status: string | null }) {
  if (!status) return null;
  const label = INJURY_BADGE_LABELS[status] ?? status.slice(0, 4).toUpperCase();
  return (
    <span className="injury-badge" title={`Sleeper: ${status}`}>
      {label}
    </span>
  );
}

/** Plan A5: per-source values side by side, without a refetch. Kept to a hover tooltip rather
 * than a new column -- A4 asked for tighter row density, and disagreement is already flagged
 * loudly via the DISAGREE badge for the cases that matter. */
function sourceBreakdown(bySource: Record<string, number> | null | undefined): string | undefined {
  if (!bySource) return undefined;
  const entries = Object.entries(bySource);
  if (entries.length < 2) return undefined;
  return entries.map(([k, v]) => `${k}: ${v.toFixed(1)}`).join("  ·  ");
}

/** Ledger #1: the spread across sources, shown on the row.
 *
 * Marc toggled blend/Sleeper and saw "they barely move at the top". He was right about the
 * ORDER -- measured, the top 12 shift 1.0-1.6 ranks on average, because the sources genuinely
 * agree there. But the VALUES move a lot even where the order does not (blend to Sleeper, top 12:
 * Gibbs 169 to 144, Jonathan Taylor 137 to 105), and QB moves least of all in absolute terms,
 * which is exactly the tab he was on.
 *
 * So the fix is not to the model, it is to show the disagreement WITHOUT requiring him to toggle
 * and hold two screens in his head. A range on the row says "these four services agree about this
 * player" or "they do not" at a glance. The "blend" entry is excluded: it is an average OF the
 * others, so including it would narrow the range it is meant to describe.
 *
 * REPORTED AS A PERCENTAGE, and that is not a styling choice. `value_by_source` carries league
 * SEASON POINTS, deliberately -- a per-source draft value would not be comparable row to row,
 * because dv depends on the whole pool's replacement level (CLAUDE.md). The column it sits next
 * to shows DRAFT VALUE. So printing a raw points spread beside a dv figure would invite reading
 * "106.6 ± 22" when the 22 is on a different scale entirely (Allen's sources span 358-380 points
 * against a dv of 106.6). A percentage of the mean is scale-free and cannot be misread as dv.
 */
function sourceSpread(
  bySource: Record<string, number> | null | undefined,
): { lo: number; hi: number; span: number; pct: number } | undefined {
  if (!bySource) return undefined;
  const vals = Object.entries(bySource)
    .filter(([k]) => k !== "blend")
    .map(([, v]) => v);
  if (vals.length < 2) return undefined;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  if (mean <= 0) return undefined;
  return { lo, hi, span: hi - lo, pct: ((hi - lo) / mean) * 100 };
}

/** A rejection Marc adjudicated in the review queue that actually changed this row's value.
 * Always visible: a decision of his is never silently folded into a number, so the board can
 * explain why it disagrees with the raw sources (docs/REVIEW_QUEUE.md). */
function DecisionBadge({ decisions }: { decisions: AppliedDecision[] | null | undefined }) {
  if (!decisions || decisions.length === 0) return null;
  const detail = decisions
    .map((d) => `${d.source} ${d.stat} rejected ${d.date}${d.reason ? " -- " + d.reason : ""}`)
    .join("  |  ");
  return (
    <span className="decision-badge" title={"Your review-queue decision: " + detail}>
      REJ{decisions.length > 1 ? `×${decisions.length}` : ""}
    </span>
  );
}

/** Marc's manual playing-time override, when it actually moved this row's expected games
 * (backend: draftroom/valuation/playing_time.py). Shown for the same reason REJ is: a games
 * figure that came from him must never read as a model output. The tooltip states what the
 * pipeline would have used, so the row explains the difference rather than just asserting one --
 * and an upward clamp is called out, because that is the one case where the number on the board
 * is NOT the number he wrote. */
function PlayingTimeBadge({ pt }: { pt: AppliedPlayingTime | null | undefined }) {
  if (!pt) return null;
  const before = pt.source_published_games
    ? pt.was.toFixed(2)
    : `${pt.was.toFixed(2)} (fitted prior -- this source publishes no games)`;
  const clamp = pt.clamped
    ? `  |  CLAMPED: you set ${pt.requested_games.toFixed(2)}, capped at the ${pt.curve.toFixed(
        2,
      )} healthy-rank curve figure`
    : "";
  const tag = pt.designation ? `${pt.designation} -- ` : "";
  return (
    <span
      className="playing-time-badge"
      title={
        `Your playing-time override (${pt.date}): ${tag}${pt.reason}` +
        `  |  expected games ${before} -> ${pt.games.toFixed(2)}${clamp}` +
        "  |  games only -- your per-game projection is untouched"
      }
    >
      {pt.games.toFixed(1)}G
    </span>
  );
}

/** A board row plus the position key it was filed under. `TierRow` itself carries no position
 * (the board is keyed by it), and the merged ALL view needs it per row. */
type PositionedRow = TierRow & { __pos: string };

/** Ledger #10: research that is NOT in the numbers.
 *
 * Every other badge on this row means a value MOVED: REJ says a source was thrown out, NN.NG
 * says a games figure was overridden. This one means the opposite, and that inversion is the
 * whole point of it existing -- something is known about this player and the value beside it
 * does not include that knowledge.
 *
 * It exists because suspension and discipline have ZERO sources in this pipeline (docs/
 * FINAL_PREP.md says so outright). The availability job can research a finding, write it to
 * data/injury_research.json with a citation, and then have nowhere to put it: injury_sweep.py
 * can only act on a finding that carries a NUMBER, so "under review, no timeline" produced no
 * override, no badge, and no trace anywhere Marc would see it in a live room.
 *
 * NOTHING about the valuation changes. The label distinguishes the two cases because he acts on
 * them differently: UNPRICED means nobody can put a number on it, and the figure means the
 * research HAS a number that is not being applied (deferred, or clamped away).
 */
function ResearchNoteBadge({
  note,
  rowName,
}: {
  note: ResearchNote | null | undefined;
  rowName: string;
}) {
  if (!note) return null;
  const label = note.games_missed === null ? "RISK" : `-${note.games_missed}G?`;
  const missed =
    note.games_missed === null
      ? "no games figure -- unpriced"
      : `research says he misses ${note.games_missed}`;
  // The join is by Sleeper id, so a VALID id for the WRONG player binds cleanly and puts one
  // man's risk on another man's row. The backend warns in the log; a log is not visible in a
  // room, so the mismatch is said here too. Compared loosely because names legitimately differ
  // on suffixes and punctuation -- this is a prompt to check, not an assertion of a bug.
  const mismatch =
    note.player_name &&
    rowName &&
    note.player_name.replace(/[^a-z]/gi, "").toLowerCase() !==
      rowName.replace(/[^a-z]/gi, "").toLowerCase();
  return (
    <span
      className="research-badge"
      title={
        (mismatch
          ? `!! THIS FINDING NAMES ${note.player_name}, NOT ${rowName} -- the research file's ` +
            `player_id may be wrong. Check before acting on it.  |  `
          : "") +
        `${note.status || "Researched finding"} (reported ${note.report_date}` +
        `${note.confidence ? `, confidence ${note.confidence}` : ""}): ${missed}.` +
        `  |  ${note.why_unpriced}` +
        (note.notes ? `  |  ${note.notes}` : "") +
        `  |  ${note.citation}`
      }
    >
      {mismatch ? `${label}?!` : label}
    </span>
  );
}

/** Ledger #8: the late-round IR stash.
 *
 * This league carries `BN x6, IR x2` (data/league_manual.yaml, confirmed). So a player already on
 * IR can be drafted late, moved straight to an IR slot, and the bench spot comes back -- two
 * lottery tickets that cost no roster space. Marc raised it himself, with the scope attached:
 * "obviously not when you have real needs."
 *
 * That caveat IS the gate. The hint appears only once every one of his own starter slots is
 * filled, which is his stated condition rather than a round number somebody picked -- this repo
 * does not ship thresholds nobody derived. NOTHING about the valuation changes: the hint is a
 * note, the player's value is whatever the board already said, and the decision stays his.
 */
function StashHint({ status, active }: { status: string | null; active: boolean }) {
  if (!active || !status) return null;
  if (!/^(IR|PUP|NA|Out)$/i.test(status.trim())) return null;
  return (
    <span
      className="stash-badge"
      title={
        `Already designated ${status}. This league has 2 IR slots, so drafting him now and ` +
        "moving him straight to IR costs no bench spot -- a free flyer. Shown only because your " +
        "own starting slots are already filled; his value on this board is unchanged."
      }
    >
      STASH
    </span>
  );
}

export function TierBoard({
  board,
  filter,
  onSelectFilter,
  pickNoByPlayerId,
  onOpenDraftMenu,
  onDraftToClock,
  onRequestUndraft,
  slotOnClockLabel,
  stashHintActive,
  vonaByPos,
  gatedIds,
  forcedPositions,
}: {
  board: Record<string, TierRow[]>;
  filter: BoardFilter;
  onSelectFilter: (p: BoardFilter) => void;
  // Plan A2: click anywhere. Undrafted rows open the team-draft popover; drafted rows get an
  // "x" that undoes them (confirming first unless it's the most recent pick -- see App.tsx).
  pickNoByPlayerId: Record<string, number>;
  onOpenDraftMenu: (e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => void;
  // Ledger #5: double-click drafts straight to the team on the clock, no popover, no confirm.
  // Marc's actual ask ("I want to be able to double click or do something to draft him"), and
  // the reason it matters is arithmetic: 150 picks x one extra confirm click, under time
  // pressure, in a room.
  onDraftToClock: (playerId: string, playerName: string) => void;
  onRequestUndraft: (e: ReactMouseEvent<HTMLElement>, pickNo: number, playerName: string) => void;
  // Whose turn it is, for the double-click tooltip -- so the shortcut says where the pick goes
  // BEFORE it is used rather than after.
  slotOnClockLabel: string;
  // Ledger #8: true once every one of Marc's own starter slots is filled, which is HIS stated
  // condition for when an IR stash is worth a late pick. Computed in App from `my_starter_fill`.
  stashHintActive: boolean;
  // Ledger #12 -- what the ALL view needs to rank by BEST PICK NOW rather than by season value.
  // All of it comes off the live recommendation payload, and App only passes it through while
  // that payload describes the board on screen; empty is the honest degraded state (no
  // recommendation yet, a failed or superseded fetch, the final round, placeholder mode) and the
  // ALL view then falls back to plain draft value.
  vonaByPos: Record<string, number>;
  // The panel's gated candidates IN THE PANEL'S OWN ORDER. Order is load-bearing: it is the only
  // thing that guarantees the top of this list equals the top of the panel, because `value+VONA`
  // cannot reproduce `utility` in general (pair optimisation at the turn, candidate-specific
  // continuation and risk terms mid-round).
  gatedIds: string[];
  // Explanatory only, for the note above the list. Never a sort key.
  forcedPositions: string[];
}) {
  // The pool is 980 players (189 ranked, 791 with no projection -- CLAUDE.md). Ranked-only is
  // the useful default view; the toggle reveals the full write-in universe for a late-round
  // deep sleeper lookup without cluttering the board the rest of the draft.
  const [showUnranked, setShowUnranked] = useState(false);

  // Ledger #2 -- the ALL view. Draft value is value above the replacement player AT THAT
  // POSITION under this league's own roster rules, so it is already a cross-position currency
  // rather than a re-scaling.
  //
  // Ledger #12 -- but it is the SEASON-value currency, and Marc asked for "the ranking of our
  // best pick now." Those differ by exactly one term, VONA: the points given up by waiting one
  // turn at that position. So the ALL view now ranks by `value + VONA(pos)`.
  //
  // Two things about this are worth not rediscovering. FIRST, `value + VONA` reproduces the
  // recommendation engine's own `utility` ordering EXACTLY -- measured on the 2026 board at
  // 1.01, all 16 candidates in the same order -- because utility is
  // `value + VONA + continuation - lam*SD` and the continuation term is position-agnostic by
  // construction, measuring 93.1-96.7 across every candidate (a ~1.8-point band on a
  // 340-point scale). So the board gets the panel's ranking without running the panel's Monte
  // Carlo for 199 players.
  //
  // SECOND, and this is the part that is easy to get backwards: the VONA term alone does NOT
  // lift the quarterback. Measured on the 2026 board at 1.01, RB's VONA is 74.5 against QB's
  // 58.7 -- RB thins out faster before Marc's next turn -- so ranking purely by pick-now value
  // moves Josh Allen from 9th to ELEVENTH. What actually puts him first in the panel is a HARD
  // GATE, not a price: the elite-QB grab (a top-3 board QB is available and Marc has 0 of 2) and
  // the deterministic scarcity floor. So the gate is part of this sort too, or the ALL view
  // would contradict the panel in the opposite direction from the one Marc complained about.
  //
  // The gate is applied at PLAYER grain where it has one (`eliteIds`, 3 players at 1.01) and at
  // POSITION grain only where the scarcity floor genuinely fires -- which by construction means
  // that position is nearly exhausted, so it can never hoist a large block. Both are badged
  // `REC`, because a re-ordering nobody can explain is worse than the disagreement it fixed.
  //
  // Tier separators are suppressed here on purpose: a tier is a within-position construct ("who
  // else is roughly as good AT THIS POSITION"), so drawing tier lines across a merged list would
  // group players whose tiers mean different things.
  // The board is KEYED by position, so the key is the authoritative position for every row in
  // it -- carried onto the row here rather than added to the server payload, because
  // reconstructing it from the key cannot disagree with the key.
  const isAll = filter === ALL_FILTER;

  // Ledger #13 -- positional rank, which Marc asked to see beside the position ("our RB1, our
  // RB2"). Ranked BY DRAFT VALUE, explicitly sorted here rather than read off the list's own
  // order: `tier_board()` serves each position in **ADP order**, so the index would have been an
  // ADP rank sitting next to the DV and NOW columns. That mismatch is visible on the real board
  // -- by ADP, Lamar Jackson is the third QB and Drake Maye the second, while by draft value
  // (69.1 vs 65.9) it is the other way round, and the same swap hits Cook/Henry at RB.
  //
  // Only ranked, really-valued players get a number: an unranked write-in has no projection, so
  // "WR47" would be an evaluation the board is explicitly refusing to make.
  const posRank = new Map<string, string>();
  for (const [pos, posRows] of Object.entries(board)) {
    posRows
      .filter((r) => r.is_ranked && r.value_is_real)
      .sort((a, b) => b.value - a.value)
      .forEach((r, i) => posRank.set(r.player_id, `${pos}${i + 1}`));
  }

  // Ledger #12: the pick-now figure the ALL view ranks by. Falls back to plain draft value when
  // there is no VONA to add (final round, placeholder mode), which is why `hasVona` is checked
  // before any of it is shown -- a "NOW" column equal to DV on every row would imply the wait
  // costs nothing rather than that it is unknown.
  // ONE readiness flag for the whole pick-now treatment -- the sort, the NOW column, the REC
  // badges and the note. Previously the column checked `hasVona` while the gate did not, so the
  // final round (empty VONA, gates still published) would have re-ordered the board with no NOW
  // column and no note explaining it (Codex 2026-08-26).
  //
  // "Ready" also requires at least one NON-ZERO price. At a back-to-back turn every position's
  // VONA is legitimately 0.0 -- nothing can be taken in a gap of zero picks, so waiting costs
  // nothing -- and the keys are still present. Treating that as ready rendered a NOW column
  // holding exactly DV on all 199 rows, which reads as a broken column rather than as the real
  // and useful fact that at the turn there is nothing to lose by waiting. That fact gets said in
  // words below instead (found on the live board 2026-08-26, Marc at slot 1 previewing pick 20).
  const vonaValues = Object.values(vonaByPos);
  const atTheTurnNoCost = vonaValues.length > 0 && vonaValues.every((v) => v === 0);
  const pickNowReady = isAll && vonaValues.some((v) => v !== 0);
  const pickNowOf = (r: PositionedRow): number =>
    r.is_ranked && r.value_is_real ? r.value + (vonaByPos[r.__pos] ?? 0) : r.value;

  // The panel's hard gate, taken from the panel's own candidate list rather than re-derived.
  // `gatedIds` is already feasibility-filtered and top-N-per-position, which is what bounds how
  // much this can hoist; `forcedPositions` would have hoisted the entire position. Drafted
  // players are excluded so an already-taken elite QB cannot sit at the top of the board wearing
  // a REC badge for the rest of the draft.
  const gateRank = new Map(gatedIds.map((id, i) => [id, i]));
  const isGated = (r: PositionedRow): boolean =>
    pickNowReady && !r.drafted && r.is_ranked && r.value_is_real && gateRank.has(r.player_id);

  const allRows: PositionedRow[] = isAll
    ? Object.entries(board)
        .flatMap(([pos, posRows]) => posRows.map((r) => ({ ...r, __pos: pos })))
        .sort((a, b) => {
          // Undrafted first, then the panel's hard gate, then by PICK-NOW value, then by ADP so
          // unranked players (value 0.0, which is "no projection" and never an evaluation) fall
          // to the bottom in a stable order. This whole comparator is ledger #12.
          if (a.drafted !== b.drafted) return a.drafted ? 1 : -1;
          const ga = isGated(a);
          const gb = isGated(b);
          if (ga !== gb) return ga ? -1 : 1;
          // Both gated: keep the PANEL'S order exactly, so the top of this list is the panel's
          // list. This is the guarantee `value + VONA` could not give.
          if (ga && gb) {
            return (gateRank.get(a.player_id) ?? 0) - (gateRank.get(b.player_id) ?? 0);
          }
          const pa = pickNowOf(a);
          const pb = pickNowOf(b);
          if (pb !== pa) return pb - pa;
          return a.adp - b.adp;
        })
    : (board[filter] ?? []).map((r) => ({ ...r, __pos: filter }));
  const rows = showUnranked ? allRows : allRows.filter((r) => r.is_ranked);
  const hiddenCount = allRows.length - rows.length;

  let lastTier: number | null | undefined = undefined;
  return (
    <div className="board-panel panel">
      <div className="board-tabs">
        {BOARD_FILTERS.map((p) => (
          <button
            key={p}
            className={`board-tab ${p} ${p === filter ? "active" : ""}`}
            onClick={() => onSelectFilter(p)}
            title={
              p === ALL_FILTER
                ? "Every position together, ranked by BEST PICK NOW: draft value plus the points given up by waiting one turn at that position. Players the recommendation ranks first regardless of value are hoisted to the top and badged REC. Falls back to plain draft value if no live recommendation is available."
                : undefined
            }
          >
            {p}
          </button>
        ))}
        <button
          className={`board-tab unranked-toggle ${showUnranked ? "active" : ""}`}
          onClick={() => setShowUnranked((v) => !v)}
          title="Show every draftable name, including players with no projection"
        >
          {showUnranked ? "Hide unranked" : `+${hiddenCount} unranked`}
        </button>
      </div>
      {/* Ledger #12/#13: the board had no column header at all, which is why Marc read the left
          column as "the ranking number" when it is ADP. Six short labels, and the ALL view's
          extra NOW column is what it now sorts by. */}
      <div className={`board-colhead ${pickNowReady ? "all-view" : ""}`}>
        <span title="Our rank on this board -- position in the list you are looking at, top to bottom. Not ADP; ADP has its own column.">
          #
        </span>
        <span>Player</span>
        <span>Team</span>
        <span title="Average draft position in the national 2QB feed -- where the ROOM takes him, not where we value him. A big gap between # and ADP is the whole edge.">
          ADP
        </span>
        <span title="Draft value: expected points above this league's replacement player at his position, over the season">
          DV
        </span>
        {pickNowReady && (
          <span title="Best pick NOW: draft value plus VONA, the points given up by waiting one turn at his position. This is what the ALL view is sorted by.">
            NOW
          </span>
        )}
        <span />
      </div>
      <div className="board-list">
        {/* Ledger #12: a re-ordering nobody can explain is worse than the disagreement it fixed,
            so when the gate has hoisted anything the list says so in words. */}
        {/* At a back-to-back turn there is genuinely no cost to waiting, so the pick-now ranking
            collapses to draft value. Said out loud, because a board that silently stops
            re-ordering looks like a board that broke. */}
        {isAll && atTheTurnNoCost && (
          <div className="board-gate-note">
            You pick twice in a row, so nothing can be taken in between and waiting costs nothing
            at any position. Ranked by draft value here.
          </div>
        )}
        {pickNowReady && rows.some((r) => isGated(r)) && (
          <div className="board-gate-note">
            Top {rows.filter((r) => isGated(r)).length} ranked first by the recommendation
            regardless of value
            {forcedPositions.length > 0
              ? ` — ${forcedPositions.join("/")} supply is about to run short`
              : " — elite QB available and you have none yet"}
            . Everything below is ordered by pick-now value.
          </div>
        )}
        {rows.map((r, idx) => {
          const showSeparator = !isAll && !r.drafted && r.tier !== lastTier && r.tier !== null;
          if (!r.drafted) lastTier = r.tier;
          return (
            <div key={r.player_id}>
              {showSeparator && (
                <div className="tier-separator">Tier {(r.tier ?? 0) + 1}</div>
              )}
              <div
                className={`board-row ${pickNowReady ? "all-view" : ""} ${r.drafted ? "drafted" : ""} ${!r.is_ranked ? "unranked" : ""}`}
              >
                {/* OUR rank on this board: the row's position in the list actually on screen,
                    1..N, so the pick the engine leads with reads as 1. This cell used to hold
                    ADP, which is why an ADP-ordered column looked like random numbers sitting
                    where a rank belongs (Marc, 2026-08-26). ADP kept, in its own labelled column. */}
                <span className="row-rank">{idx + 1}</span>
                <span className="row-name">
                  {r.drafted ? (
                    <span
                      className="clickable-name"
                      title="Click to change or undraft"
                      onClick={(e) => {
                        const pickNo = pickNoByPlayerId[r.player_id];
                        if (pickNo !== undefined) onRequestUndraft(e, pickNo, r.name);
                      }}
                    >
                      {r.name}
                    </span>
                  ) : (
                    /* Ledger #16, 2026-08-27. These two gestures were the other way round:
                       a single click opened the team picker and a DOUBLE click drafted to the
                       clock. Both paths already existed; the bug was which one was easiest.
                       Marc: "as long as i keep up i should do it in order" -- the in-order pick
                       is what happens ~150 times in a room with people talking, and the team
                       picker is the CATCH-UP tool for when he "fell behind and hear[s] that
                       someone 3 picks later picked someone".
                       So: LEFT CLICK drafts to whoever is on the clock. RIGHT CLICK (or the
                       small pick-a-team affordance beside the name) opens the picker. A misclick
                       is recoverable -- Ctrl+Z undoes, and the newest pick's "x" undrafts
                       instantly without a confirm. */
                    <>
                      <span
                        className="clickable-name draftable"
                        title={`Click to draft to ${slotOnClockLabel} (on the clock). Right-click to pick a different team.`}
                        onClick={(e) => {
                          e.stopPropagation();
                          onDraftToClock(r.player_id, r.name);
                        }}
                        onContextMenu={(e) => onOpenDraftMenu(e, r.player_id, r.name)}
                      >
                        {r.name}
                      </span>
                      {/* Right-click is not discoverable on its own, and this is the only way
                          into an out-of-order pick. A visible affordance beside the name keeps
                          the catch-up path findable without making it the easy one. */}
                      <button
                        type="button"
                        className="pick-team-btn"
                        title="Draft to a different team (catching up, or an out-of-order pick)"
                        aria-label={`Draft ${r.name} to a specific team`}
                        onClick={(e) => onOpenDraftMenu(e, r.player_id, r.name)}
                      >
                        &#9662;
                      </button>
                    </>
                  )}
                  {/* Ledger #13: positional rank, not just the position. An unranked write-in
                      keeps the bare position -- it has no projection to rank. */}
                  {isAll && (
                    <span className={`pos-badge ${r.__pos}`}>
                      {posRank.get(r.player_id) ?? r.__pos}
                    </span>
                  )}
                  {/* Ledger #12: the panel's hard gates, badged rather than applied to this
                      sort. Says WHY the panel can lead with someone this list puts lower. */}
                  {isAll && isGated(r) && (
                    <span
                      className="rec-first-badge"
                      title={
                        forcedPositions.includes(r.__pos)
                          ? `Scarcity floor: startable ${r.__pos}s are about to run short of the league's open ${r.__pos} slots, so the recommendation panel ranks ${r.__pos} first regardless of value. This list is still ordered by pick-now value.`
                          : `The recommendation panel ranks him first regardless of value (elite QB available and you have none yet). This list is still ordered by pick-now value.`
                      }
                    >
                      REC
                    </span>
                  )}
                  {r.disagreement_high && (
                    <span
                      className="danger-badge"
                      title={sourceBreakdown(r.value_by_source) ?? "High cross-source disagreement -- a danger signal, not a recommendation"}
                    >
                      DISAGREE
                    </span>
                  )}
                  <DecisionBadge decisions={r.projection_decisions} />
                  <PlayingTimeBadge pt={r.playing_time} />
                  <ResearchNoteBadge note={r.research_note} rowName={r.name} />
                  <InjuryBadge status={r.injury_status} />
                  <StashHint status={r.injury_status} active={stashHintActive} />
                  {r.drafted && pickNoByPlayerId[r.player_id] !== undefined && (
                    <button
                      className="undraft-x"
                      title="Undraft this pick"
                      aria-label={`Undraft ${r.name}`}
                      onClick={(e) => onRequestUndraft(e, pickNoByPlayerId[r.player_id], r.name)}
                    >
                      &times;
                    </button>
                  )}
                </span>
                <span className="row-team">{r.team}{r.bye ? ` bye ${r.bye}` : ""}</span>
                {/* ADP in its own column now that # is our rank. Kept because the GAP between the
                    two is the edge in this league: Josh Allen is ADP 1.7 and our #1, but Lamar is
                    ADP 20-something and our #2. */}
                <span
                  className="row-adpval"
                  title={
                    r.is_ranked
                      ? `The room takes him around pick ${r.adp.toFixed(1)} on average (national 2QB ADP). Compare that to the # column, which is where WE have him.`
                      : "Not in the ADP feed -- listed for bookkeeping only, no projection"
                  }
                >
                  {r.is_ranked ? r.adp.toFixed(0) : "—"}
                </span>
                <span
                  className="row-adp"
                  title={sourceBreakdown(r.value_by_source)}
                >
                  {(() => {
                    const s = sourceSpread(r.value_by_source);
                    if (!s || !r.is_ranked || !r.value_is_real) return null;
                    return (
                      <span
                        className={`source-spread ${s.pct >= 20 ? "wide" : ""}`}
                        title={
                          `The four projection services span ${s.lo.toFixed(0)}-${s.hi.toFixed(0)} SEASON POINTS on him, ` +
                          `a ${s.pct.toFixed(0)}% spread. Shown as a percentage because those are season points and the ` +
                          `number beside it is draft value -- different scales. A narrow spread means they agree; ` +
                          `it is NOT evidence they are right.`
                        }
                      >
                        ±{s.pct.toFixed(0)}%
                      </span>
                    );
                  })()}
                  {!r.is_ranked
                    ? "no proj"
                    : r.value_is_real
                      ? r.value.toFixed(0)
                      : r.value > 0
                        ? /* placeholder fallback mode: ADP-derived stand-in, starred so it
                             never reads as the validated model */ `${r.value.toFixed(0)}*`
                        : /* ranked but excluded from the real board: name kept for
                             bookkeeping, no evaluation to show */ "—"}
                </span>
                {/* Ledger #12: the pick-now figure this view is sorted by, kept BESIDE draft
                    value rather than replacing it -- seeing both is the point, because the gap
                    between them IS the cost of waiting at his position. */}
                {pickNowReady && (
                  <span className="row-picknow">
                    {r.is_ranked && r.value_is_real ? (
                      <span
                        title={
                          `${r.value.toFixed(1)} draft value + ${(vonaByPos[r.__pos] ?? 0).toFixed(1)} ` +
                          `cost of waiting one turn at ${r.__pos} = ${pickNowOf(r).toFixed(1)}. ` +
                          `A bigger gap means his position thins out faster before your next pick.`
                        }
                      >
                        {pickNowOf(r).toFixed(0)}
                      </span>
                    ) : (
                      "—"
                    )}
                  </span>
                )}
                <span className="row-owner">{r.drafted ? r.owner_label : ""}</span>
              </div>
            </div>
          );
        })}
        {rows.length === 0 && <div className="empty-hint">No players loaded at {filter}.</div>}
      </div>
    </div>
  );
}
