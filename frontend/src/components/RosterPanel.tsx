import type { MouseEvent as ReactMouseEvent } from "react";
import type { DraftState } from "../types";
import { DemandClockPanel } from "./DemandClockPanel";

export function RosterPanel({
  state,
  onStartJumpClock,
  onRequestUndraft,
}: {
  state: DraftState;
  onStartJumpClock: () => void;
  // Plan A2: replaces the old window.prompt("Void which pick number?") entirely -- every roster
  // row (mine and every opponent's) gets its own "x" instead. Shared with the tier board and
  // search results so the confirm-unless-most-recent rule lives in exactly one place (App.tsx).
  onRequestUndraft: (e: ReactMouseEvent<HTMLElement>, pickNo: number, playerName: string) => void;
}) {
  const fill = state.my_starter_fill;

  return (
    <div className="roster-panel panel">
      <div className="roster-section">
        <h3>Board Controls</h3>
        <div className="board-controls">
          <button className="control-btn" onClick={onStartJumpClock} title="Ctrl+G">
            Jump Clock
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
              <button
                className="undraft-x"
                title="Undraft this pick"
                aria-label={`Undraft ${p.name}`}
                onClick={(e) => onRequestUndraft(e, p.pick_no, p.name ?? "this player")}
              >
                &times;
              </button>
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
                      <button
                        className="undraft-x"
                        title="Undraft this pick"
                        aria-label={`Undraft ${p.name}`}
                        onClick={(e) => onRequestUndraft(e, p.pick_no, p.name ?? "this player")}
                      >
                        &times;
                      </button>
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
