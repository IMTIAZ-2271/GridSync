/**
 * Where the nav's unread dot gets its numbers.
 *
 * One entry per list that carries an indicator in the sub-nav: the query that
 * fetches it, and which field on a row is the arrival time to compare against
 * the account's watermark.
 *
 * **The query keys are the same ones the pages use**, so React Query dedupes:
 * when you are on the page there is no extra request at all, and when you are
 * on a sibling page there is one small one. That is the whole reason the dot
 * is driven from the real list rather than from a server-side count -- a count
 * endpoint would have to re-implement each list's scoping rules beside the
 * list itself, and the two would drift.
 *
 * Site-scoped lists (a household's bills, its meters) are deliberately absent.
 * They depend on which site is selected, so a dot for them would either be
 * wrong for the other sites or need every site fetched to render one glyph.
 * Those pages still highlight their own new rows on open; they just do not
 * advertise from the nav.
 */
import { api, queryKeys } from "./api";
import { VIEWS, type ViewKey } from "./unread";

export interface UnreadSource {
  queryKey: readonly unknown[];
  queryFn: () => Promise<unknown[]>;
  /** The row's arrival time. Rows with none are never counted as unread. */
  timestampOf: (row: never) => string | null | undefined;
}

type Row = Record<string, unknown>;
const field = (name: string) => (row: never) =>
  (row as Row)[name] as string | null | undefined;

export const UNREAD_SOURCES: Partial<Record<ViewKey, UnreadSource>> = {
  // --- consumer ----------------------------------------------------------
  [VIEWS.consumerApplications]: {
    queryKey: queryKeys.netMeteringApplications(),
    queryFn: api.netMeteringApplications,
    timestampOf: field("created_at"),
  },
  [VIEWS.consumerIssues]: {
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
    timestampOf: field("reported_at"),
  },
  [VIEWS.consumerVisits]: {
    queryKey: queryKeys.visits(),
    queryFn: api.visits,
    // A visit becomes news when it is finished and needs a verdict -- not when
    // it was raised, which is the office's bookkeeping and not the
    // household's.
    timestampOf: field("completed_at"),
  },

  // --- worker ------------------------------------------------------------
  [VIEWS.workerOrders]: {
    queryKey: queryKeys.workOrders(),
    queryFn: api.listWorkOrders,
    timestampOf: field("created_at"),
  },
  [VIEWS.workerIssues]: {
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
    timestampOf: field("reported_at"),
  },

  // --- government --------------------------------------------------------
  [VIEWS.governmentAgreements]: {
    queryKey: queryKeys.pendingAgreements(),
    queryFn: api.pendingAgreements,
    timestampOf: field("created_at"),
  },
  [VIEWS.governmentMeterApplications]: {
    // Undecided only, matching the page's default -- same cache entry, and
    // the right set for an indicator about work still waiting.
    queryKey: queryKeys.meterApplicationQueue(false),
    queryFn: () => api.meterApplicationQueue(false),
    timestampOf: field("submitted_at"),
  },
  [VIEWS.governmentWorkers]: {
    queryKey: queryKeys.pendingWorkers(),
    queryFn: api.pendingWorkers,
    timestampOf: field("registered_at"),
  },
  [VIEWS.governmentSupplierRegistrations]: {
    queryKey: queryKeys.pendingSupplierRegistrations(),
    queryFn: api.pendingSupplierRegistrations,
    timestampOf: field("registered_at"),
  },

  // --- supplier ----------------------------------------------------------
  [VIEWS.supplierApplications]: {
    // openOnly, matching the page's own default -- so the dot's query and the
    // page's are the same cache entry and no extra request is made. It is also
    // the right set to count: an inbox indicator is about work still waiting,
    // not about applications already decided.
    queryKey: queryKeys.solarApplications(true),
    queryFn: () => api.solarApplications(true),
    timestampOf: field("submitted_at"),
  },
  [VIEWS.supplierIssues]: {
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
    timestampOf: field("reported_at"),
  },
  [VIEWS.supplierDispatch]: {
    queryKey: queryKeys.dispatchableIssues(),
    queryFn: api.dispatchableIssues,
    timestampOf: field("reported_at"),
  },
};
