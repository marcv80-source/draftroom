import type { MouseEvent as ReactMouseEvent } from "react";
import type { Candidate, Recommendation } from "../types";

export interface PlayerFlagInfo {
  disagreement_high: boolean;
  injury_status: string | null;
}

function pct(p: number): string {
  if (p >= 0.995) return ">99%";
  if (p > 0 && p < 0.01) return "<1%";
  return `${Math.round(p * 100)}%`;
}

function CandidateCard({
  c,
  maxValue,
  flags,
  onOpenDraftMenu,
}: {
  c: Candidate;
  maxValue: number;
  flags?: PlayerFlagInfo;
  onOpenDraftMenu: (e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => void;
}) {
  const width = maxValue > 0 ? Math.max(4, (c.draft_value / maxValue) * 100) : 4;
  return (
    <div className="candidate-card">
      <div className="candidate-top">
        <span className={`pos-badge ${c.pos}`}>{c.pos}</span>
        <span
          className="candidate-name clickable-name"
          title="Click to draft to a team"
          onClick={(e) => onOpenDraftMenu(e, c.player_id, c.name)}
        >
          {c.name}
        </span>
        {flags?.disagreement_high && (
          <span
            className="danger-badge"
            title="High cross-source disagreement -- a danger signal, not a recommendation"
          >
            DISAGREE
          </span>
        )}
        {flags?.injury_status && <span className="injury-badge">{flags.injury_status}</span>}
        <span className="tier-badge">Tier {c.tier.tier_index + 1}</span>
      </div>
      <div className="value-bar-track">
        <div className="value-bar-fill" style={{ width: `${width}%` }} />
      </div>
      <div className="survival-pct">
        {pct(c.survival.p_survive_next)} survives to {c.survival.next_pick_label} &middot; value{" "}
        {c.draft_value.toFixed(0)}
      </div>
      {c.bullets.length > 0 && (
        <ul className="bullets">
          {c.bullets.slice(0, 3).map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function RecommendationPanel({
  rec,
  mode,
  playerFlags,
  eliteQbCutoff,
  onEliteQbCutoffChange,
  onOpenDraftMenu,
}: {
  rec: Recommendation | null;
  mode: "clock" | "mine";
  playerFlags?: Record<string, PlayerFlagInfo>;
  eliteQbCutoff: number;
  onEliteQbCutoffChange: (n: number) => void;
  onOpenDraftMenu: (e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => void;
}) {
  const maxValue = rec ? Math.max(1, ...rec.candidates.map((c) => c.draft_value)) : 1;
  return (
    <div className="rec-panel panel">
      <div className="rec-header">
        <h2>Recommendation</h2>
        <span className="rec-mode-toggle">
          {mode === "clock" ? "team on the clock" : "my next pick"} (F2)
        </span>
      </div>

      {/* Ledger #6. The engine used to say nothing at all until it was literally Marc's turn --
          "why is it not giving a recommendation? ... it should be making the case for why we
          should be picking Josh Allen." It now answers for his own next pick at any time, and
          that answer has to be LABELLED, because it is computed against who is available right
          now and does not simulate the picks in between being taken. A preview presented as live
          would be a worse failure than the silence it replaces. */}
      {rec && rec.preview_for_pick != null && (
        <div className="rec-preview-banner">
          Preview for <strong>your pick {rec.pick_label}</strong>
          {rec.picks_away ? ` - ${rec.picks_away} pick${rec.picks_away === 1 ? "" : "s"} away` : ""},
          based on who is available <strong>right now</strong>. Players between here and there will
          go; each card's survival number is the odds that one lasts.
        </div>
      )}

      <div className="elite-qb-knob">
        <label htmlFor="elite-qb-cutoff">Elite QB grab: top</label>
        <input
          id="elite-qb-cutoff"
          type="number"
          min={0}
          max={12}
          value={eliteQbCutoff}
          onChange={(e) => {
            const n = parseInt(e.target.value, 10);
            onEliteQbCutoffChange(Number.isFinite(n) ? Math.max(0, n) : 0);
          }}
        />
        <span className="command-hint">{eliteQbCutoff === 0 ? "off" : "QBs (0 = off)"}</span>
      </div>

      {!rec && <div className="empty-hint">Loading...</div>}

      {rec && rec.warnings.map((w, i) => (
        <div className="warning-banner" key={i}>
          {w}
        </div>
      ))}

      {rec && rec.candidates.length === 0 && (
        <div className="empty-hint">
          No ranked candidates yet for pick {rec.pick_label}. The model informs, it never
          insists -- use the tier board and command bar in the meantime.
        </div>
      )}

      {rec &&
        rec.candidates.map((c) => (
          <CandidateCard
            key={c.player_id}
            c={c}
            maxValue={maxValue}
            flags={playerFlags?.[c.player_id]}
            onOpenDraftMenu={onOpenDraftMenu}
          />
        ))}
    </div>
  );
}
