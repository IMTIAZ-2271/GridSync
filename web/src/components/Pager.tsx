/** Previous / next over an offset-paged list, with "41–90 of 132". */
export default function Pager({
  total,
  offset,
  pageSize,
  onOffset,
}: {
  total: number;
  offset: number;
  pageSize: number;
  onOffset: (offset: number) => void;
}) {
  if (total <= pageSize) return null;
  const last = Math.min(offset + pageSize, total);
  return (
    <div className="flex items-center justify-between gap-4 border-t border-hairline px-5 py-3 text-sm">
      <p className="tabular text-ink-muted">
        {offset + 1}–{last} of {total}
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={offset === 0}
          onClick={() => onOffset(Math.max(0, offset - pageSize))}
          className="rounded-md border border-hairline px-3 py-1.5 text-ink-2 disabled:opacity-40"
        >
          Previous
        </button>
        <button
          type="button"
          disabled={last >= total}
          onClick={() => onOffset(offset + pageSize)}
          className="rounded-md border border-hairline px-3 py-1.5 text-ink-2 disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  );
}
