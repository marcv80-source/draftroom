import type { DraftState } from "../types";

export function RosterPanel({ state }: { state: DraftState }) {
  const fill = state.my_starter_fill;
  return (
    <div className="roster-panel panel">
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
        <h3>Opponents (QB slots highlighted)</h3>
        <div className="opponent-grid">
          {state.opponents
            .filter((o) => !o.is_mine)
            .map((o) => (
              <div className={`opponent-row ${o.is_mine ? "mine" : ""}`} key={o.team_slot}>
                <span>{o.team_label}</span>
                <span className={`opponent-qb ${o.qb_unfilled > 0 ? "needs" : "ok"}`}>
                  QB {o.qb_count}
                </span>
                <span className="opponent-counts">
                  {Object.entries(o.counts)
                    .filter(([pos]) => pos !== "QB")
                    .map(([pos, n]) => `${pos}${n}`)
                    .join(" ")}
                </span>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
