import { useEffect, useMemo, useRef, useState } from "react";

/**
 * A searchable dropdown for choosing what a chart or a stat tile is about.
 *
 * Consumer requirement 4 asks for filtering by "overall usage, individual
 * meters, specific connections". In this schema those are not three lists: a
 * *connection* is a `billing_point`, and rule 7 gives each point exactly one
 * active billing meter, so a meter and the connection it serves are the same
 * row seen from two sides. Two dropdowns would have been two copies of one
 * choice. Each option therefore carries both names -- the connection's label
 * and the serial of the meter measuring it -- and either one finds it when
 * typed.
 *
 * Deliberately not a native <select>: the point of the requirement is that a
 * household with several connections can *search*, and a select cannot be
 * filtered. Deliberately not a combobox library either -- this is one control
 * with one job, and a dependency for it would outweigh it.
 */

export interface ScopeOption {
  id: string | null;
  label: string;
  /** Second line, and also searchable: usually the meter serial. */
  detail?: string;
}

export default function ScopePicker({
  options,
  value,
  onChange,
  label = "Scope",
}: {
  options: ScopeOption[];
  value: string | null;
  onChange: (id: string | null) => void;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selected =
    options.find((o) => o.id === value) ?? options[0] ?? null;

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) =>
        o.label.toLowerCase().includes(q) ||
        (o.detail ?? "").toLowerCase().includes(q),
    );
  }, [options, query]);

  // Close on an outside click or Escape. Both are registered only while the
  // panel is open, so a page full of these does not pay for listeners it is
  // not using.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
    else setQuery("");
  }, [open]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={label}
        className="flex min-w-44 max-w-64 items-center justify-between gap-2 rounded-md border border-hairline bg-surface px-3 py-1.5 text-left text-xs text-ink transition-colors hover:bg-plane"
      >
        <span className="truncate">
          <span className="font-medium">{selected?.label ?? "—"}</span>
          {selected?.detail && (
            <span className="ml-1.5 text-ink-muted">{selected.detail}</span>
          )}
        </span>
        <span aria-hidden className="text-ink-muted">
          ▾
        </span>
      </button>

      {open && (
        <div className="absolute right-0 z-20 mt-1 w-72 overflow-hidden rounded-lg border border-hairline bg-surface shadow-lg">
          <div className="border-b border-hairline p-2">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search connections and meters…"
              className="w-full rounded-md border border-hairline bg-plane px-2.5 py-1.5 text-xs text-ink outline-none focus:border-series-import"
            />
          </div>
          <ul role="listbox" className="max-h-64 overflow-y-auto py-1">
            {matches.length === 0 && (
              <li className="px-3 py-2 text-xs text-ink-muted">
                Nothing matches “{query}”.
              </li>
            )}
            {matches.map((o) => {
              const isSelected = o.id === (selected?.id ?? null);
              return (
                <li key={o.id ?? "all"}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    onClick={() => {
                      onChange(o.id);
                      setOpen(false);
                    }}
                    className={`flex w-full items-baseline gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-plane ${
                      isSelected ? "bg-plane font-medium text-ink" : "text-ink-2"
                    }`}
                  >
                    <span className="truncate">{o.label}</span>
                    {o.detail && (
                      <span className="ml-auto shrink-0 font-mono text-ink-muted">
                        {o.detail}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
