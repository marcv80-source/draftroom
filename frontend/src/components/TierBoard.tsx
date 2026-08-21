import { useState } from "react";
import type { MouseEvent as ReactMouseEvent } from "react";
import { POSITIONS, type AppliedDecision, type Position, type TierRow } from "../types";

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

export function TierBoard({
  board,
  filter,
  onSelectFilter,
  pickNoByPlayerId,
  onOpenDraftMenu,
  onRequestUndraft,
}: {
  board: Record<string, TierRow[]>;
  filter: Position;
  onSelectFilter: (p: Position) => void;
  // Plan A2: click anywhere. Undrafted rows open the team-draft popover; drafted rows get an
  // "x" that undoes them (confirming first unless it's the most recent pick -- see App.tsx).
  pickNoByPlayerId: Record<string, number>;
  onOpenDraftMenu: (e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => void;
  onRequestUndraft: (e: ReactMouseEvent<HTMLElement>, pickNo: number, playerName: string) => void;
}) {
  // The pool is 980 players (189 ranked, 791 with no projection -- CLAUDE.md). Ranked-only is
  // the useful default view; the toggle reveals the full write-in universe for a late-round
  // deep sleeper lookup without cluttering the board the rest of the draft.
  const [showUnranked, setShowUnranked] = useState(false);

  const allRows = board[filter] ?? [];
  const rows = showUnranked ? allRows : allRows.filter((r) => r.is_ranked);
  const hiddenCount = allRows.length - rows.length;

  let lastTier: number | null | undefined = undefined;
  return (
    <div className="board-panel panel">
      <div className="board-tabs">
        {POSITIONS.map((p) => (
          <button
            key={p}
            className={`board-tab ${p} ${p === filter ? "active" : ""}`}
            onClick={() => onSelectFilter(p)}
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
          const showSeparator = !r.drafted && r.tier !== lastTier && r.tier !== null;
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
                      className="clickable-name"
                      title="Click to draft to a team"
                      onClick={(e) => onOpenDraftMenu(e, r.player_id, r.name)}
                    >
                      {r.name}
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
                  <InjuryBadge status={r.injury_status} />
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
