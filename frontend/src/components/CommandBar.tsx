import type { RefObject } from "react";
import type { SearchMatch } from "../types";

export function CommandBar({
  modeLabel,
  placeholder,
  value,
  onChange,
  matches,
  highlightedIndex,
  inputRef,
  error,
}: {
  modeLabel: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  matches: SearchMatch[];
  highlightedIndex: number;
  inputRef: RefObject<HTMLInputElement>;
  error?: string | null;
}) {
  return (
    <div className="command-bar">
      <div className="command-mode-label" style={error ? { color: "var(--danger)" } : undefined}>
        {error || modeLabel}
      </div>
      <div className="command-row">
        <input
          ref={inputRef}
          className="command-input"
          autoFocus
          value={value}
          placeholder={placeholder}
          onChange={(e) => onChange(e.target.value)}
          onBlur={(e) => {
            // The command bar is always focused -- Marc never touches the mouse mid-draft.
            e.target.focus();
          }}
        />
      </div>
      {matches.length > 0 && (
        <div className="command-results">
          {matches.map((m, i) => (
            <div
              key={m.player_id}
              className={`command-match ${i === highlightedIndex ? "highlighted" : ""} ${
                m.drafted ? "drafted" : ""
              }`}
            >
              <span className={`pos-badge ${m.pos}`}>{m.pos}</span>
              <span>{m.name}</span>
              <span className="command-hint">{m.team}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
