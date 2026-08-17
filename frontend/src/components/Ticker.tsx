import type { UpcomingPick } from "../types";

export function Ticker({ picks }: { picks: UpcomingPick[] }) {
  return (
    <div className="ticker">
      {picks.map((p) => {
        const classes = ["ticker-pick"];
        if (p.is_mine) classes.push("mine");
        if (p.is_on_clock) classes.push("on-clock");
        if (p.filled) classes.push("filled");
        return (
          <div key={p.pick_no} className={classes.join(" ")}>
            <div className="label">{p.pick_label}</div>
            <div className="team">{p.team_label}</div>
            {p.is_mine && !p.is_on_clock && (
              <div className="you-in">◄ YOU IN {p.pick_no - picks[0].pick_no}</div>
            )}
            {p.is_mine && p.is_on_clock && <div className="you-in">◄ ON THE CLOCK</div>}
          </div>
        );
      })}
    </div>
  );
}
