const BINDINGS: Array<[string, string]> = [
  ["Enter", "Draft the highlighted match to whoever is on the clock"],
  ["Up / Down", "Move the highlight in the command bar's results"],
  ["Esc", "Clear the command bar, then close any open modal"],
  ["Tab / Shift+Tab", "Cycle the tier board's position filter"],
  ["Ctrl+Z", "Undo the last pick"],
  ["Ctrl+E", "Edit a past pick"],
  ["Ctrl+G", "Jump the clock to a pick number"],
  ["@N (suffix)", "Assign the highlighted player to team N out of order"],
  ["Ctrl+U", "Add an unknown player as a stub, then one key for position (Q/R/W/T)"],
  ["~ (prefix)", "Include already-drafted players in search results"],
  ["F1", "Toggle this help overlay"],
  ["F2", "Toggle recommendation: my next pick vs. team on the clock"],
  ["Jump Clock (button)", "Same as Ctrl+G -- resync the clock to a pick number"],
  ["+N unranked (button)", "Show every draftable name on the tier board, including no-projection players"],
  ["Click a name (undrafted)", "Board, search results, or a recommendation candidate -- opens a team picker, one more click drafts"],
  ["x on a drafted name", "Undrafts that pick. Confirms first unless it's the most recent pick"],
  ["Draft Results (header)", "The whole draft in pick order, grouped by round; voided picks stay visible, struck through"],
  ["Right-click a results row (or the ⋮ button)", "Remove / Replace with... / Reassign to team..."],
  ["Team Names (header)", "Name all 10 slots and reorder them fast as the draw happens at the table"],
  ["SOURCE (header)", "Switch the projection source the whole board is ranked on -- always shown, never implicit"],
];

export function HelpOverlay({ onClose }: { onClose: () => void }) {
  return (
    <div className="overlay-backdrop" onClick={onClose}>
      <div className="help-card" onClick={(e) => e.stopPropagation()}>
        <h2>Keys</h2>
        {BINDINGS.map(([k, desc]) => (
          <div className="help-row" key={k}>
            <span className="help-key">{k}</span>
            <span>{desc}</span>
          </div>
        ))}
        <p className="footer-note">
          Fantasy data provided by Yahoo Fantasy. ADP from Fantasy Football Calculator. Press
          F1 or Esc to close.
        </p>
      </div>
    </div>
  );
}
