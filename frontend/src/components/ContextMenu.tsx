import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** A positioned popup menu anchored at a viewport point (a click or right-click), used for
 * both the click-anywhere draft/undraft popover (A2) and the Draft Results row menu (A3).
 *
 * - Opens at (x, y), then nudges itself back on-screen so it never renders off a viewport edge.
 * - Closes on outside click. Escape is handled by the CALLER (see App.tsx's onEscape) rather
 *   than a second listener here, so it composes correctly with the app's other Escape behavior
 *   (clear search / close help) instead of racing it -- two independent capture-phase keydown
 *   listeners on window fire in registration order, and the app's listener is registered once
 *   at mount, before any menu ever opens.
 * - Keyboard reachable: every action is a real <button>, and the first one gets focus on open. */
export function ContextMenu({
  x,
  y,
  onClose,
  children,
}: {
  x: number;
  y: number;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<{ left: number; top: number; visibility: "visible" | "hidden" }>({
    left: x,
    top: y,
    visibility: "hidden",
  });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const margin = 8;
    let left = x;
    let top = y;
    if (left + rect.width + margin > window.innerWidth) {
      left = Math.max(margin, window.innerWidth - rect.width - margin);
    }
    if (top + rect.height + margin > window.innerHeight) {
      top = Math.max(margin, window.innerHeight - rect.height - margin);
    }
    setStyle({ left, top, visibility: "visible" });
    const firstButton = el.querySelector<HTMLButtonElement>("button");
    firstButton?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [x, y]);

  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    // Capture phase so this fires even if a child stops propagation on bubble.
    window.addEventListener("mousedown", onDown, true);
    return () => window.removeEventListener("mousedown", onDown, true);
  }, [onClose]);

  return (
    <div
      ref={ref}
      className="context-menu"
      role="menu"
      style={{ position: "fixed", left: style.left, top: style.top, visibility: style.visibility }}
      onClick={(e) => e.stopPropagation()}
    >
      {children}
    </div>
  );
}
