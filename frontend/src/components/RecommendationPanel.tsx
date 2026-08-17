import type { Candidate, Recommendation } from "../types";

function pct(p: number): string {
  if (p >= 0.995) return ">99%";
  if (p > 0 && p < 0.01) return "<1%";
  return `${Math.round(p * 100)}%`;
}

function CandidateCard({ c, maxValue }: { c: Candidate; maxValue: number }) {
  const width = maxValue > 0 ? Math.max(4, (c.draft_value / maxValue) * 100) : 4;
  return (
    <div className="candidate-card">
      <div className="candidate-top">
        <span className={`pos-badge ${c.pos}`}>{c.pos}</span>
        <span className="candidate-name">{c.name}</span>
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
}: {
  rec: Recommendation | null;
  mode: "clock" | "mine";
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
        rec.candidates.map((c) => <CandidateCard key={c.player_id} c={c} maxValue={maxValue} />)}
    </div>
  );
}
