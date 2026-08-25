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
              {/* Ledger #7. This used to read "21 left / vs 20 needed" and nothing else, which
                  Marc could not parse -- "I don't really understand the rationale of what we're
                  saying in that demand clock" -- and he was right: two bare numbers with no
                  subject and no verb. The numbers were always correct, and the QB line is the
                  single most important thing on this screen (21 startable quarterbacks for 20
                  starting quarterback JOBS is the entire reason a 2-QB league breaks every public
                  ranking). So it now says it, in words, on the row rather than in a tooltip
                  nobody hovers on draft night. */}
              <div className="demand-clock-numbers">
                <span className="demand-clock-supply">{e.startable_remaining}</span>
                <span className="demand-clock-vs">for</span>
                <span className="demand-clock-demand">{e.league_demand_remaining}</span>
              </div>
              <div className="demand-clock-sentence">
                {e.startable_remaining} startable {e.position}
                {e.startable_remaining === 1 ? "" : "s"} left for{" "}
                {e.league_demand_remaining} starting {e.position} job
                {e.league_demand_remaining === 1 ? "" : "s"} in the league
              </div>
              <div className={`demand-clock-cushion ${tight ? "tight" : ""}`}>
                {e.cushion > 0
                  ? `${e.cushion} spare`
                  : e.cushion === 0
                    ? "NO spare - exactly enough to go round"
                    : `SHORT by ${Math.abs(e.cushion)} - someone will not fill this slot`}
              </div>
              <div className="demand-clock-detail">
                {e.teams_needing_before_next_turn > 0
                  ? `${e.teams_needing_before_next_turn} team${e.teams_needing_before_next_turn === 1 ? "" : "s"} still need one before your turn`
                  : e.picks_before_next_turn > 0
                    ? "none of the teams before your turn need one"
                    : "you're on the clock"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
