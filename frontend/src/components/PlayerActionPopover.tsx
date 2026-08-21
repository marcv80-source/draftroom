import { ContextMenu } from "./ContextMenu";

export type ActionMenuState =
  | { kind: "draft"; playerId: string; playerName: string; x: number; y: number }
  | { kind: "confirm-void"; pickNo: number; playerName: string; x: number; y: number };

export interface TeamOption {
  team_slot: number;
  team_label: string;
}

/** Plan A2: click an undrafted name anywhere -> pick a team (one more click commits, no
 * confirmation -- Ctrl+Z undoes). Click the "x" on a drafted name that is NOT the most recent
 * pick -> this same popover in its confirm form, because voiding mid-board rewrites history.
 * (The most-recent pick's "x" skips this entirely and votes immediately -- see App.tsx.) */
export function PlayerActionPopover({
  menu,
  teams,
  defaultTeamSlot,
  onClose,
  onDraft,
  onConfirmVoid,
}: {
  menu: ActionMenuState;
  teams: TeamOption[];
  defaultTeamSlot: number;
  onClose: () => void;
  onDraft: (playerId: string, teamSlot: number) => void;
  onConfirmVoid: (pickNo: number) => void;
}) {
  return (
    <ContextMenu x={menu.x} y={menu.y} onClose={onClose}>
      {menu.kind === "draft" ? (
        <>
          <div className="menu-title">Draft {menu.playerName} to...</div>
          <div className="menu-scroll">
            {teams
              .slice()
              // The team on the clock sorts first, so ContextMenu's "focus the first button"
              // lands on the suggested default -- one Enter commits the common case.
              .sort((a, b) =>
                a.team_slot === defaultTeamSlot
                  ? -1
                  : b.team_slot === defaultTeamSlot
                    ? 1
                    : a.team_slot - b.team_slot,
              )
              .map((t) => (
                <button
                  key={t.team_slot}
                  className={`menu-item ${t.team_slot === defaultTeamSlot ? "suggested" : ""}`}
                  onClick={() => onDraft(menu.playerId, t.team_slot)}
                >
                  {t.team_label}
                  {t.team_slot === defaultTeamSlot ? " (on the clock)" : ""}
                </button>
              ))}
          </div>
        </>
      ) : (
        <>
          <div className="menu-title">Void pick #{menu.pickNo}?</div>
          <div className="menu-note">
            {menu.playerName} returns to the pool. This isn't the most recent pick, so it
            rewrites history mid-board.
          </div>
          <div className="menu-actions">
            <button className="menu-item danger" onClick={() => onConfirmVoid(menu.pickNo)}>
              Confirm void
            </button>
            <button className="menu-item" onClick={onClose}>
              Cancel
            </button>
          </div>
        </>
      )}
    </ContextMenu>
  );
}
