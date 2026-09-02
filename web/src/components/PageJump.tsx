import { useEffect, useState } from "react";

// The page numeral in every pager is this input: type a number, Enter (or
// blur) jumps there. Strictly numeric — non-digits never enter the field.
// Escape restores the current page without jumping.

interface Props {
  page: number;
  /** Total pages; 0 or undefined = unknown (no upper clamp). */
  total?: number;
  onJump: (page: number) => void;
}

export function PageJump({ page, total = 0, onJump }: Props) {
  const [text, setText] = useState(String(page));
  useEffect(() => { setText(String(page)); }, [page]);

  const commit = () => {
    const n = parseInt(text, 10);
    if (!text || Number.isNaN(n) || n < 1) { setText(String(page)); return; }
    const target = total > 0 ? Math.min(n, total) : n;
    setText(String(target));
    if (target !== page) onJump(target);
  };

  return (
    <input
      className="pagejump"
      type="text"
      inputMode="numeric"
      value={text}
      style={{ width: `${Math.max(2, String(total || page).length) + 1}ch` }}
      onChange={e => setText(e.target.value.replace(/\D/g, ""))}
      onKeyDown={e => {
        if (e.key === "Enter") { commit(); e.currentTarget.blur(); }
        else if (e.key === "Escape") { setText(String(page)); e.currentTarget.blur(); }
      }}
      onBlur={commit}
      onFocus={e => e.currentTarget.select()}
      onClick={e => e.stopPropagation()}
      aria-label="Jump to page"
      title="Type a page number and press Enter"
    />
  );
}
