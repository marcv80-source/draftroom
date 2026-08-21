import type { MouseEvent as ReactMouseEvent, RefObject } from "react";
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
  pickNoByPlayerId,
  onOpenDraftMenu,
  onRequestUndraft,
}: {
  modeLabel: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  matches: SearchMatch[];
  highlightedIndex: number;
  inputRef: RefObject<HTMLInputElement>;
  error?: string | null;
  // Plan A2: search results are click-anywhere too -- an undrafted chip opens the team-draft
  // popover, a drafted one (when we know its pick_no) gets an "x" to undraft.
  pickNoByPlayerId?: Record<string, number>;
  onOpenDraftMenu?: (e: ReactMouseEvent<HTMLElement>, playerId: string, playerName: string) => void;
  onRequestUndraft?: (e: ReactMouseEvent<HTMLElement>, pickNo: number, playerName: string) => void;
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
          {matches.map((m, i) => {
            const pickNo = pickNoByPlayerId?.[m.player_id];
            return (
              <div
                key={m.player_id}
                className={`command-match ${i === highlightedIndex ? "highlighted" : ""} ${
                  m.drafted ? "drafted" : ""
                }`}
              >
                <span className={`pos-badge ${m.pos}`}>{m.pos}</span>
                <span
                  className={onOpenDraftMenu && !m.drafted ? "clickable-name" : undefined}
                  title={!m.drafted ? "Click to draft to a team" : undefined}
                  onClick={(e) => {
                    if (!m.drafted && onOpenDraftMenu) onOpenDraftMenu(e, m.player_id, m.name);
                  }}
                >
                  {m.name}
                </span>
                <span className="command-hint">{m.team}</span>
                {m.drafted && pickNo !== undefined && onRequestUndraft && (
                  <button
                    className="undraft-x"
                    title="Undraft this pick"
                    aria-label={`Undraft ${m.name}`}
                    onClick={(e) => onRequestUndraft(e, pickNo, m.name)}
                  >
                    &times;
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
