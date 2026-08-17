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
