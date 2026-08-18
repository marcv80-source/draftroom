import { POSITIONS, type DemandClockEntry } from "../types";

/** Compact per-position supply-vs-demand panel. Informs, never recommends (CLAUDE.md tone
 * contract): no position here is ranked or suggested, just the numbers Marc would otherwise
 * have to count in his head mid-conversation. */
export function DemandClockPanel({ clock }: { clock: Record<string, DemandClockEntry> }) {
  const entries = POSITIONS.map((p) => clock[p]).filter((e): e is DemandClockEntry => Boolean(e));
  if (entries.length === 0) return null;

  return (
    <div className="demand-clock">
      <h3>Demand Clock</h3>
      <div className="demand-clock-grid">
        {entries.map((e) => {
          const tight = e.cushion <= 0;
          return (
            <div className={`demand-clock-cell ${tight ? "tight" : ""}`} key={e.position}>
              <div className="demand-clock-pos">
                <span className={`pos-badge ${e.position}`}>{e.position}</span>
              </div>
              <div className="demand-clock-numbers">
                <span title="Ranked, startable players left at this position">
                  {e.startable_remaining} left
                </span>
                <span title="Unfilled starter slots across the whole league">
                  vs {e.league_demand_remaining} needed
                </span>
              </div>
              <div className="demand-clock-detail">
                {e.teams_needing_before_next_turn > 0
                  ? `${e.teams_needing_before_next_turn} team${e.teams_needing_before_next_turn === 1 ? "" : "s"} need it before your turn`
                  : e.picks_before_next_turn > 0
                    ? "none of the teams before your turn need it"
                    : "you're on the clock"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
