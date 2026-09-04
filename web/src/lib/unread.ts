/**
 * Read/unread for lists.
 *
 * The server stores one watermark per account per list -- when that account
 * last opened it -- and nothing else. Whether a given row is unread is decided
 * here, on the client, by comparing the row's own timestamp against the
 * watermark.
 *
 * That split is deliberate. Counting unread rows on the server would mean
 * re-implementing every list's scoping rules (which sites a consumer owns,
 * which complaints a supplier is near, which district an official governs) as
 * a second query beside the first, and the two would drift the moment either
 * changed. The list is the authority on what is in the list.
 *
 * The behaviour it produces, which is the point:
 *
 *   1. A row arrives newer than the watermark -> the nav shows a red dot.
 *   2. The page is opened. It marks itself seen and is handed back the
 *      watermark it just replaced, so it can light those rows light blue --
 *      they are visible on exactly the visit that clears them.
 *   3. The page is opened again. The watermark has moved past them, so they
 *      render normally and the dot is gone.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, queryKeys, type ViewState } from "./api";

/**
 * Every list that carries an indicator. Must match VIEW_KEYS in
 * services/api/routes_views.py -- the server answers 422 to anything else, so
 * a typo here is a visible failure rather than an indicator that never clears.
 */
export const VIEWS = {
  consumerApplications: "consumer:applications",
  consumerMeters: "consumer:meters",
  consumerBills: "consumer:bills",
  consumerIssues: "consumer:issues",
  consumerVisits: "consumer:visits",
  workerOrders: "worker:orders",
  workerIssues: "worker:issues",
  governmentAgreements: "government:agreements",
  governmentMeterApplications: "government:meter-applications",
  governmentWorkers: "government:workers",
  governmentSupplierRegistrations: "government:supplier-registrations",
  supplierApplications: "supplier:applications",
  supplierIssues: "supplier:issues",
  supplierDispatch: "supplier:dispatch",
} as const;

export type ViewKey = (typeof VIEWS)[keyof typeof VIEWS];

/** Every watermark this account holds, as a lookup. */
export function useViewWatermarks() {
  const query = useQuery({
    queryKey: queryKeys.viewStates(),
    queryFn: api.viewStates,
    // Cheap and small, but it does not need to be fresh to the second: the
    // dot appearing a few seconds late is invisible, and re-fetching it on
    // every focus would be noise.
    staleTime: 30_000,
  });
  const byKey = useMemo(() => {
    const out: Record<string, string> = {};
    for (const v of (query.data ?? []) as ViewState[]) out[v.view_key] = v.last_viewed_at;
    return out;
  }, [query.data]);
  return { watermarks: byKey, isLoading: query.isLoading };
}

/**
 * Mark a list seen on open, and return the watermark that was replaced.
 *
 * The returned value is frozen for the life of the page. If it were reactive,
 * a background refetch would move it forward and the highlight would vanish
 * while the reader was still looking at the row -- which is precisely the
 * moment it is meant to be visible.
 *
 * Returns `undefined` until the call lands, so a caller can tell "not known
 * yet" from `null`, which means this account has never opened the list and
 * every row in it is new.
 */
export function useMarkViewSeen(viewKey: ViewKey) {
  const client = useQueryClient();
  const [watermark, setWatermark] = useState<string | null | undefined>(undefined);
  const fired = useRef(false);

  const { mutate } = useMutation({
    mutationFn: () => api.markViewSeen(viewKey),
    onSuccess: (result) => {
      setWatermark(result.previous_viewed_at);
      // The nav's dot is derived from the same watermarks, so it has to be
      // told: otherwise the page clears itself and the dot beside its own nav
      // item stays lit until something else happens to refetch.
      client.invalidateQueries({ queryKey: queryKeys.viewStates() });
    },
    onError: () => {
      // A failed mark-seen must not break the page. Treating it as "nothing is
      // new" is the safe direction: a missed highlight is a cosmetic loss,
      // whereas lighting every row would look like a fault.
      setWatermark(null);
    },
  });

  useEffect(() => {
    // Once per mount. React 18's StrictMode double-invokes effects in dev, and
    // a second call would move the watermark again and hand back a value a few
    // milliseconds old -- highlighting nothing.
    if (fired.current) return;
    fired.current = true;
    mutate();
  }, [mutate]);

  return watermark;
}

/**
 * Is this row newer than the watermark?
 *
 * `null` watermark means never opened, so everything is new. `undefined` means
 * not known yet, and nothing is highlighted -- better a highlight that arrives
 * a moment late than a page that flashes every row on load.
 */
export function isUnread(
  rowTimestamp: string | null | undefined,
  watermark: string | null | undefined,
): boolean {
  if (watermark === undefined) return false;
  if (!rowTimestamp) return false;
  if (watermark === null) return true;
  return new Date(rowTimestamp).getTime() > new Date(watermark).getTime();
}

/** How many rows in this list are newer than the account's watermark. */
export function countUnread<T>(
  rows: readonly T[] | undefined,
  timestampOf: (row: T) => string | null | undefined,
  watermark: string | null | undefined,
): number {
  if (!rows?.length || watermark === undefined) return 0;
  return rows.reduce((n, row) => (isUnread(timestampOf(row), watermark) ? n + 1 : n), 0);
}

/**
 * The row treatment. A wash plus a left edge, never the wash alone: the app's
 * own rule is that status does not travel by colour, and a 1.03:1 tint is
 * invisible to plenty of people and to anyone printing the page.
 */
export const UNREAD_ROW_CLASS =
  "bg-unread shadow-[inset_3px_0_0_0_var(--color-unread-edge)]";

export function unreadRowClass(unread: boolean): string {
  return unread ? UNREAD_ROW_CLASS : "";
}
