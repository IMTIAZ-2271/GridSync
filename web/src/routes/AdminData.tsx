import { useEffect, useMemo, useRef, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type BrowseQuery,
  type BrowseTable,
  type BrowseValue,
} from "../lib/api";
import Pager from "../components/Pager";
import { Badge, Card, CardHeader, EmptyState, ErrorState, Skeleton } from "../components/ui";

const PAGE = 50;

/**
 * Every table, read-only.
 *
 * Nothing here can change a row -- the API has no write method for it, and
 * each read runs in a read-only transaction. Credential hashes arrive as null
 * and show as "hidden"; NUMERIC values arrive as their exact strings and are
 * shown exactly as stored (rule 5). `device_reading` only opens filtered by a
 * device, because it holds 48 rows a day for every meter and inverter.
 */
export default function AdminData() {
  const [search, setSearch] = useState("");
  const [tableName, setTableName] = useState<string | null>(null);

  const tables = useQuery({ queryKey: queryKeys.adminTables(), queryFn: api.adminTables });

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (tables.data ?? []).filter((t) => t.name.includes(needle));
  }, [tables.data, search]);

  const table = tables.data?.find((t) => t.name === tableName) ?? null;

  // On a narrow screen the rows sit below the table list: bring them into view
  // once they have rendered, or picking a table appears to do nothing. Instant,
  // not smooth -- a smooth scroll is cut short when the grid grows under it.
  const panel = useRef<HTMLDivElement>(null);
  const revealPanel = () => {
    if (window.matchMedia("(max-width: 1023px)").matches) {
      panel.current?.scrollIntoView({ block: "start" });
    }
  };

  return (
    <div className="grid items-start gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <Card>
        <CardHeader
          title="Tables"
          subtitle={tables.data ? `${tables.data.length} in the database` : undefined}
        />
        <div className="border-b border-hairline px-3 py-2">
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Find a table"
            aria-label="Find a table"
            className="w-full rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
          />
        </div>
        {tables.isPending ? (
          <div className="space-y-2 p-3">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        ) : tables.error ? (
          <ErrorState error={tables.error} />
        ) : (
          <ul className="max-h-[32rem] overflow-y-auto py-1 lg:max-h-[70vh]">
            {shown.map((t) => (
              <li key={t.name}>
                <button
                  type="button"
                  onClick={() => setTableName(t.name)}
                  aria-current={t.name === tableName ? "true" : undefined}
                  className={`flex w-full items-baseline justify-between gap-2 px-4 py-1.5 text-left text-sm hover:bg-plane ${
                    t.name === tableName ? "bg-plane font-medium text-ink" : "text-ink-2"
                  }`}
                >
                  <span className="truncate font-mono text-xs">{t.name}</span>
                  <span className="tabular shrink-0 text-xs text-ink-muted">
                    {t.approx_rows === null ? "" : `~${t.approx_rows.toLocaleString()}`}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {table ? (
        // Keyed on the table so its filter and page reset when it changes.
        <div ref={panel} className="min-w-0 scroll-mt-4">
          <TableView key={table.name} table={table} onShown={revealPanel} />
        </div>
      ) : (
        <Card>
          <EmptyState
            title="Pick a table"
            hint="Rows are read-only. Credential hashes are never shown."
          />
        </Card>
      )}
    </div>
  );
}

function TableView({ table, onShown }: { table: BrowseTable; onShown: () => void }) {
  const filterable = table.columns.filter((c) => !c.masked);
  const [filterCol, setFilterCol] = useState(table.required_filter ?? filterable[0]?.name ?? "");
  const [draft, setDraft] = useState("");
  const [applied, setApplied] = useState<{ col: string; val: string } | null>(null);
  const [offset, setOffset] = useState(0);
  const [descending, setDescending] = useState(false);

  const needsFilter = table.required_filter !== null && applied === null;
  const query: BrowseQuery = {
    limit: PAGE,
    offset,
    descending: descending || undefined,
    filter_col: applied?.col,
    filter_val: applied?.val,
  };
  const rows = useQuery({
    queryKey: queryKeys.adminTableRows(table.name, query),
    queryFn: () => api.adminTableRows(table.name, query),
    enabled: !needsFilter,
    placeholderData: keepPreviousData,
    retry: false,
  });

  // Once per table: when its first page -- or its "filter first" notice -- is up.
  const shown = needsFilter || rows.data !== undefined || rows.error !== null;
  useEffect(() => {
    if (shown) onShown();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown]);

  const typeOf = Object.fromEntries(table.columns.map((c) => [c.name, c]));
  const rowError =
    rows.error instanceof ApiError && typeof rows.error.detail === "string"
      ? rows.error.detail
      : rows.error?.message;

  return (
    <Card className="min-w-0">
      <CardHeader
        title={table.name}
        subtitle={
          rows.data
            ? `${rows.data.total.toLocaleString()} ${applied ? "matching" : "rows"} · ${table.columns.length} columns`
            : `${table.columns.length} columns`
        }
        action={<Badge tone="neutral">read-only</Badge>}
      />

      <form
        className="flex flex-wrap items-end gap-3 border-b border-hairline px-5 py-3"
        onSubmit={(e) => {
          e.preventDefault();
          setOffset(0);
          setApplied(draft.trim() ? { col: filterCol, val: draft.trim() } : null);
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-ink-muted">
          Column
          <select
            value={filterCol}
            onChange={(e) => setFilterCol(e.target.value)}
            disabled={table.required_filter !== null}
            className="rounded-md border border-hairline bg-surface px-2 py-1.5 font-mono text-sm text-ink"
          >
            {filterable.map((c) => (
              <option key={c.name} value={c.name}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-0 flex-1 basis-40 flex-col gap-1 text-xs text-ink-muted">
          Equals
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={typeOf[filterCol]?.type ?? ""}
            className="rounded-md border border-hairline bg-surface px-2 py-1.5 font-mono text-sm text-ink"
          />
        </label>
        <button
          type="submit"
          disabled={!draft.trim() && !applied}
          className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-40"
        >
          {!draft.trim() && applied ? "Clear filter" : "Filter"}
        </button>
        <label className="flex items-center gap-1.5 pb-1.5 text-xs text-ink-2">
          <input
            type="checkbox"
            checked={descending}
            onChange={(e) => {
              setDescending(e.target.checked);
              setOffset(0);
            }}
          />
          Reverse order
        </label>
      </form>

      {needsFilter ? (
        <EmptyState
          title={`Filter by ${table.required_filter} first`}
          hint="This table is too large to page through whole. Paste a device ID from the device table."
        />
      ) : rows.isPending ? (
        <div className="space-y-2 p-5">
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
          <Skeleton className="h-6 w-full" />
        </div>
      ) : rows.error ? (
        rowError && rows.error instanceof ApiError && rows.error.status === 422 ? (
          <p className="px-5 py-4 text-sm text-status-critical">{rowError}</p>
        ) : (
          <ErrorState error={rows.error} />
        )
      ) : rows.data.rows.length === 0 ? (
        <EmptyState title="No rows" hint={applied ? "Nothing matches that filter." : "This table is empty."} />
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-max min-w-full text-xs">
              <thead>
                <tr className="border-b border-hairline text-left">
                  {rows.data.columns.map((name) => (
                    <th key={name} className="px-3 py-2 align-bottom font-medium whitespace-nowrap text-ink-2">
                      <span className="font-mono">{name}</span>
                      {typeOf[name]?.primary_key && <span className="ml-1 text-ink-muted">key</span>}
                      <span className="block font-normal text-ink-muted">{typeOf[name]?.type}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.data.rows.map((row, i) => (
                  <tr key={i} className="border-b border-hairline last:border-0">
                    {rows.data.columns.map((name) => (
                      <td key={name} className="max-w-[18rem] px-3 py-1.5 align-top">
                        <Cell value={row[name]} masked={rows.data.masked.includes(name)} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager total={rows.data.total} offset={offset} pageSize={PAGE} onOffset={setOffset} />
        </>
      )}
    </Card>
  );
}

function Cell({ value, masked }: { value: BrowseValue | undefined; masked: boolean }) {
  if (masked) return <span className="text-ink-muted italic">hidden</span>;
  if (value === null || value === undefined) return <span className="text-ink-muted">null</span>;
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return (
    <span className="block truncate font-mono text-ink" title={text}>
      {text}
    </span>
  );
}
