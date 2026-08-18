import { voidPick } from "../api";
import type { DraftState } from "../types";
import { DemandClockPanel } from "./DemandClockPanel";

export function RosterPanel({
  state,
  onState,
  onError,
  onStartJumpClock,
}: {
  state: DraftState;
  onState: (s: DraftState) => void;
  onError: (msg: string | null) => void;
  onStartJumpClock: () => void;
}) {
  const fill = state.my_starter_fill;

  function handleVoid() {
    const raw = window.prompt("Void which pick number? (the player returns to the pool)");
    if (raw === null) return;
    const n = parseInt(raw.trim(), 10);
    if (!Number.isFinite(n) || n < 1) {
      onError("Enter a valid pick number to void.");
      return;
    }
    if (!window.confirm(`Void pick #${n}? This marks the pick void and frees the player. You can re-enter a correct pick at that number afterward.`)) {
      return;
    }
    onError(null);
    voidPick(n).then(onState).catch((err) => onError(String(err)));
  }

  return (
    <div className="roster-panel panel">
      <div className="roster-section">
        <h3>Board Controls</h3>
        <div className="board-controls">
          <button className="control-btn" onClick={onStartJumpClock} title="Ctrl+G">
            Jump Clock
          </button>
          <button className="control-btn danger" onClick={handleVoid}>
            Void a Pick
          </button>
        </div>
      </div>

      <DemandClockPanel clock={state.demand_clock} />

      <div className="roster-section">
        <h3>My Roster</h3>
        {Object.entries(fill.starters).map(([pos, s]) => (
          <div className="slot-row" key={pos}>
            <span className={`pos-badge ${pos}`}>{pos}</span>
            <span className={`slot-fill ${s.filled >= s.need ? "complete" : ""}`}>
              {s.filled}/{s.need}
            </span>
          </div>
        ))}
        {fill.flex.need > 0 && (
          <div className="slot-row">
            <span>FLEX</span>
            <span className={`slot-fill ${fill.flex.filled >= fill.flex.need ? "complete" : ""}`}>
              {fill.flex.filled}/{fill.flex.need}
            </span>
          </div>
        )}
        <div className="slot-row">
          <span>BENCH</span>
          <span className="slot-fill">
            {fill.bench_used}/{fill.bench_size}
          </span>
        </div>
      </div>

      <div className="roster-section">
        <h3>My Picks</h3>
        <ul className="my-roster-list">
          {state.my_roster.map((p) => (
            <li key={p.pick_no}>
              <span className="row-adp">{p.pick_label}</span>
              {p.pos && <span className={`pos-badge ${p.pos}`}>{p.pos}</span>}
              <span>{p.name}</span>
              {p.is_stub && <span className="command-hint">(stub)</span>}
            </li>
          ))}
          {state.my_roster.length === 0 && <li className="empty-hint">No picks yet.</li>}
        </ul>
      </div>

      <div className="roster-section">
        <h3>Opponents</h3>
        <div className="opponent-cards">
          {state.opponents
            .filter((o) => !o.is_mine)
            .map((o) => (
              <details className="opponent-card" key={o.team_slot}>
                <summary>
                  <span className="opponent-name">{o.team_label}</span>
                  <span className={`qb-complete-badge ${o.qb_complete ? "complete" : "open"}`}>
                    {o.qb_complete ? "QB done" : `QB ${o.qb_count}`}
                  </span>
                </summary>
                <div className="opponent-summary-line">{o.open_slots_summary}</div>
                <ul className="opponent-roster-list">
                  {o.roster.map((p) => (
                    <li key={p.pick_no}>
                      <span className="row-adp">{p.pick_label}</span>
                      {p.pos && <span className={`pos-badge ${p.pos}`}>{p.pos}</span>}
                      <span>{p.name}</span>
                      {p.is_stub && <span className="command-hint">(stub)</span>}
                    </li>
                  ))}
                  {o.roster.length === 0 && <li className="empty-hint">No picks yet.</li>}
                </ul>
              </details>
            ))}
        </div>
      </div>
    </div>
  );
}
