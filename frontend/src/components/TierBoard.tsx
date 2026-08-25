import { useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  ALL_FILTER,
  BOARD_FILTERS,
  type AppliedDecision,
  type AppliedPlayingTime,
  type BoardFilter,
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
}) {
  // The pool is 980 players (189 ranked, 791 with no projection -- CLAUDE.md). Ranked-only is
  // the useful default view; the toggle reveals the full write-in universe for a late-round
  // deep sleeper lookup without cluttering the board the rest of the draft.
  const [showUnranked, setShowUnranked] = useState(false);

  // Ledger #2 -- the ALL view. Draft value is ALREADY the cross-position common currency: it is
  // value above the replacement player AT THAT POSITION under this league's own roster rules, so
  // sorting every position together by it is an apples-to-apples ranking rather than a
  // re-scaling. That is the answer to Marc's own follow-up ("in the all, how do we rate them
  // relative to what's left") -- the scarcity is already priced into the number.
  //
  // Tier separators are suppressed here on purpose: a tier is a within-position construct ("who
  // else is roughly as good AT THIS POSITION"), so drawing tier lines across a merged list would
  // group players whose tiers mean different things.
  // The board is KEYED by position, so the key is the authoritative position for every row in
  // it -- carried onto the row here rather than added to the server payload, because
  // reconstructing it from the key cannot disagree with the key.
  const isAll = filter === ALL_FILTER;
  const allRows: PositionedRow[] = isAll
    ? Object.entries(board)
        .flatMap(([pos, posRows]) => posRows.map((r) => ({ ...r, __pos: pos })))
        .sort((a, b) => {
          // Undrafted first, then by value, then by ADP so unranked players (value 0.0, which is
          // "no projection" and never an evaluation) fall to the bottom in a stable order.
          if (a.drafted !== b.drafted) return a.drafted ? 1 : -1;
          if (b.value !== a.value) return b.value - a.value;
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
                ? "Every position together, ranked by draft value -- best available regardless of position"
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
      <div className="board-list">
        {rows.map((r) => {
          const showSeparator = !isAll && !r.drafted && r.tier !== lastTier && r.tier !== null;
          if (!r.drafted) lastTier = r.tier;
          return (
            <div key={r.player_id}>
              {showSeparator && (
                <div className="tier-separator">Tier {(r.tier ?? 0) + 1}</div>
              )}
              <div className={`board-row ${r.drafted ? "drafted" : ""} ${!r.is_ranked ? "unranked" : ""}`}>
                <span className="row-rank">{r.is_ranked ? r.adp.toFixed(0) : "—"}</span>
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
                    <span
                      className="clickable-name draftable"
                      title={`Click to pick the team - double-click to draft straight to ${slotOnClockLabel}`}
                      onClick={(e) => onOpenDraftMenu(e, r.player_id, r.name)}
                      onDoubleClick={(e) => {
                        // Stop the popover the single click just opened from lingering over the
                        // board after the double-click has already recorded the pick.
                        e.stopPropagation();
                        onDraftToClock(r.player_id, r.name);
                      }}
                    >
                      {r.name}
                    </span>
                  )}
                  {isAll && <span className={`pos-badge ${r.__pos}`}>{r.__pos}</span>}
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
