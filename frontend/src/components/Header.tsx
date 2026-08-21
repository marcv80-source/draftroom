import logoUrl from "../assets/cc-boys-logo.png";
import type { DraftState } from "../types";
import { SourceToggle } from "./SourceToggle";

/** Plan A4 rebrand + A5 header control. The logo file is 90x90 -- all Yahoo served -- so it is
 * rendered at 40px here and must never be scaled past ~64px anywhere else in the app. */
export function Header({
  state,
  onOpenTeamNames,
  boardView,
  onToggleBoardView,
  onOpenHelp,
  onState,
  onError,
}: {
  state: DraftState | null;
  onOpenTeamNames: () => void;
  boardView: "tiers" | "results";
  onToggleBoardView: () => void;
  onOpenHelp: () => void;
  onState: (s: DraftState) => void;
  onError: (msg: string | null) => void;
}) {
  return (
    <header className="app-header">
      <div className="brand">
        <img src={logoUrl} alt="" className="brand-logo" width={40} height={40} />
        <span className="brand-title">CC Boys Draft Room</span>
      </div>
      <div className="header-controls">
        <SourceToggle state={state} onState={onState} onError={onError} />
        <button className="header-btn" onClick={onOpenTeamNames}>
          Team Names
        </button>
        <button className={`header-btn ${boardView === "results" ? "active" : ""}`} onClick={onToggleBoardView}>
          {boardView === "tiers" ? "Draft Results" : "Tier Board"}
        </button>
        <button className="header-btn" onClick={onOpenHelp} title="F1">
          Help
        </button>
      </div>
    </header>
  );
}
