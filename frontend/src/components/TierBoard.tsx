import { POSITIONS, type Position, type TierRow } from "../types";

export function TierBoard({
  board,
  filter,
  onSelectFilter,
}: {
  board: Record<string, TierRow[]>;
  filter: Position;
  onSelectFilter: (p: Position) => void;
}) {
  const rows = board[filter] ?? [];

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
              <div className={`board-row ${r.drafted ? "drafted" : ""}`}>
                <span className="row-rank">{r.adp.toFixed(0)}</span>
                <span className="row-name">{r.name}</span>
                <span className="row-team">{r.team}{r.bye ? ` bye ${r.bye}` : ""}</span>
                <span className="row-adp">{r.value.toFixed(0)}</span>
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
