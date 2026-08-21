/**
 * Typed client for the GridSync API.
 *
 * Every money and energy field is a `string`, not a `number`, and that is
 * deliberate on both sides of the wire. Postgres stores them as NUMERIC and
 * CLAUDE.md forbids FLOAT for either; parsing them into a JavaScript double
 * here would reintroduce exactly the precision loss the schema avoids. So they
 * arrive as decimal strings and stay that way.
 *
 * The rule that follows: render strings directly, and call `toNumber()` only
 * at a boundary that genuinely requires a number -- a chart axis, a width
 * calculation. Never round-trip a currency figure through `Number` on its way
 * to the screen.
 */

const BASE = "/api";

// --------------------------------------------------------------------------
// Bearer token
//
// Held in localStorage, which survives a refresh and a new tab. That also
// makes it readable by any script running on this origin, so an XSS bug is a
// token theft -- the tradeoff a demo can accept and a real deployment should
// revisit (httpOnly cookie + CSRF token).
//
// The module holds the token rather than reading storage on every request, so
// AuthContext and this client cannot disagree about who is signed in.
// --------------------------------------------------------------------------

const TOKEN_KEY = "gridsync.token";

let authToken: string | null = readStoredToken();

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    // Private mode, or storage disabled. Auth still works for this tab.
    return null;
  }
}

export function setToken(token: string | null): void {
  authToken = token;
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* in-memory only */
  }
}

export function getToken(): string | null {
  return authToken;
}

/** Called when the API rejects our token, so the app can send us to /login. */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

/** A decimal string from a Postgres NUMERIC. See the note above. */
export type Decimal = string;

/** ISO-8601 timestamp, UTC. */
export type Timestamp = string;

/** ISO-8601 calendar date, no time. */
export type DateOnly = string;

// ---------------------------------------------------------------------------
// Domain types -- these mirror the response models in services/api/main.py
// ---------------------------------------------------------------------------

export interface Site {
  site_id: string;
  label: string;
  district: string;
  account_name: string;
  has_solar: boolean;
}

export interface LatestBill {
  bill_id: string;
  period_start: DateOnly;
  period_end: DateOnly;
  currency: string;
  energy_charge: Decimal;
  export_credit_earned: Decimal;
  fixed_charge: Decimal;
  tax_amount: Decimal;
  gross_amount: Decimal;
  credit_applied_kwh: Decimal;
  credit_applied_amount: Decimal;
  credit_closing_kwh: Decimal;
  amount_due: Decimal;
  due_date: DateOnly | null;
  issued_at: Timestamp;
  status: BillStatus;
}

export interface EnergyWindow {
  days: number;
  import_kwh: Decimal;
  export_kwh: Decimal;
  generation_kwh: Decimal;
  /** generation - export: what the house used from its own panels. */
  self_consumption_kwh: Decimal;
}

export interface SiteSummary {
  site_id: string;
  label: string;
  district: string;
  credit_balance_kwh: Decimal;
  credit_balance_amount: Decimal;
  last_30_days: EnergyWindow;
  /** Null for a site that has telemetry but has never been billed. */
  latest_bill: LatestBill | null;
}

export interface Reading {
  interval_start: Timestamp;
  import_kwh: Decimal;
  export_kwh: Decimal;
  generation_kwh: Decimal;
}

export type TouPeriod = "peak" | "shoulder" | "off_peak" | "flat";
export type BillLineType =
  | "energy_import"
  | "export_credit"
  | "fixed"
  | "demand"
  | "tax"
  | "adjustment";
export type BillStatus = "issued" | "partially_paid" | "paid" | "overdue" | "void";

export interface BillLineItem {
  sort_order: number;
  line_type: BillLineType;
  period_name: TouPeriod | null;
  quantity_kwh: Decimal | null;
  /** The rate frozen onto the line when the bill was cut, never re-derived. */
  rate_applied: Decimal | null;
  amount: Decimal;
}

export interface Bill {
  bill_id: string;
  period_id: string;
  period_start: DateOnly;
  period_end: DateOnly;
  /** Share of expected intervals actually received. Below 100 is worth saying. */
  coverage_pct: Decimal | null;
  /** The period's metered totals, frozen when it was billed. */
  total_import_kwh: Decimal;
  total_export_kwh: Decimal;
  total_generation_kwh: Decimal;
  currency: string;
  energy_charge: Decimal;
  export_credit_earned: Decimal;
  fixed_charge: Decimal;
  tax_amount: Decimal;
  gross_amount: Decimal;
  credit_opening_kwh: Decimal;
  credit_applied_kwh: Decimal;
  credit_applied_amount: Decimal;
  credit_closing_kwh: Decimal;
  amount_due: Decimal;
  due_date: DateOnly | null;
  issued_at: Timestamp;
  status: BillStatus;
  /** Set when this bill was superseded by a correction. */
  voided_by_bill_id: string | null;
  line_items: BillLineItem[];
}

export type IssueCategory =
  | "billing_dispute"
  | "meter_fault"
  | "inverter_fault"
  | "outage"
  | "export_not_credited"
  | "data_gap"
  | "other";
export type IssueSeverity = "low" | "medium" | "high" | "critical";
export type IssueStatus =
  | "open"
  | "acknowledged"
  | "in_progress"
  | "resolved"
  | "closed"
  | "duplicate";

export interface Issue {
  issue_id: string;
  site_id: string;
  site_label: string;
  device_id: string | null;
  bill_id: string | null;
  category: IssueCategory;
  severity: IssueSeverity;
  status: IssueStatus;
  title: string;
  description: string | null;
  priority: number;
  reported_at: Timestamp;
  acknowledged_at: Timestamp | null;
  resolved_at: Timestamp | null;
  reported_by_account_id: string;
  reported_by_name: string;
}

export interface IssueCreate {
  site_id: string;
  category: IssueCategory;
  title: string;
  description?: string | null;
  severity?: IssueSeverity;
  priority?: number;
  device_id?: string | null;
  bill_id?: string | null;
}

export type WorkOrderStatus =
  | "draft"
  | "scheduled"
  | "dispatched"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled";
export type WorkOrderType =
  | "meter_install"
  | "meter_swap"
  | "meter_removal"
  | "inverter_service"
  | "inspection"
  | "seal_check"
  | "disconnection"
  | "reconnection";
export type AssignmentRole = "lead" | "assistant" | "inspector";
export type AssignmentStatus =
  | "offered"
  | "accepted"
  | "declined"
  | "released"
  | "completed";

export interface Assignment {
  account_id: string;
  worker_name: string;
  job_role: AssignmentRole;
  status: AssignmentStatus;
  assigned_at: Timestamp;
}

export interface WorkOrder {
  order_id: string;
  site_id: string;
  site_label: string;
  issue_id: string | null;
  device_id: string | null;
  order_type: WorkOrderType;
  status: WorkOrderStatus;
  priority: number;
  scheduled_for: Timestamp | null;
  started_at: Timestamp | null;
  completed_at: Timestamp | null;
  completion_notes: string | null;
  failure_reason: string | null;
  created_at: Timestamp;
  /** A job can need more than one person; empty array if unassigned. */
  assignments: Assignment[];
}

export type SettlementType = "rollover_only" | "annual_cashout" | "net_billing";
export type AgreementStatus = "pending" | "active" | "suspended" | "terminated";

export interface Agreement {
  agreement_id: string;
  site_id: string;
  site_label: string;
  district: string;
  account_name: string;
  billing_device_id: string;
  billing_device_serial: string;
  approval_ref: string;
  sanctioned_capacity_kw: Decimal;
  export_cap_pct: Decimal;
  settlement_type: SettlementType;
  credit_rollover_months: number | null;
  effective_from: DateOnly;
  effective_to: DateOnly | null;
  status: AgreementStatus;
  created_at: Timestamp;
}

/** Only these two are reachable from a review; see the API model. */
export type AgreementDecision = "active" | "terminated";

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/**
 * `consumer` is the customer portal's role. The schema's word for a household
 * is `consumer` and the enum predates the portal naming, so the API keeps it
 * rather than migrating a live enum for cosmetics.
 */
export type Role = "consumer" | "worker" | "government" | "supplier" | "admin";

export interface Account {
  account_id: string;
  email: string;
  full_name: string;
  phone: string | null;
  role: Role;
  status: string;
  created_at: Timestamp | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  account: Account;
}

export interface LoginBody {
  email: string;
  password: string;
}

export interface CustomerRegisterBody {
  email: string;
  password: string;
  full_name: string;
  phone?: string | null;
  /** Serial printed on the site's billing meter, e.g. SEED-MTR-03. */
  meter_serial: string;
}

export interface WorkerRegisterBody {
  email: string;
  password: string;
  full_name: string;
  /** Employee code on an existing worker profile, e.g. SEED-EMP-002. */
  employee_code: string;
}

export interface StaffRegisterBody {
  email: string;
  password: string;
  full_name: string;
  registration_code: string;
}

export interface AreaStats {
  district: string;
  site_count: number;
  solar_site_count: number;
  total_import_kwh: Decimal;
  total_export_kwh: Decimal;
  total_generation_kwh: Decimal;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

/**
 * A non-2xx response.
 *
 * `detail` is carried through because the API puts something useful in it --
 * which foreign key was unknown, or why an agreement could not be decided.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(
      typeof detail === "string"
        ? detail
        : `Request failed with status ${status}`,
    );
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
      ...init?.headers,
    },
  });

  if (!res.ok) {
    // A 401 means the token is gone, expired or revoked -- there is nothing to
    // retry, so drop it and let the app redirect rather than leaving the user
    // on a page that will fail every request. 403 is different: the token is
    // valid, this role just may not do that, and signing them out would be
    // both wrong and confusing.
    if (res.status === 401 && authToken) {
      setToken(null);
      onUnauthorized?.();
    }
    // FastAPI puts a string on HTTPException and an array on a validation
    // failure. Both live under `detail`; neither is guaranteed to be JSON if
    // something upstream failed, hence the catch.
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail ?? res.statusText;
    } catch {
      /* keep the status text */
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Fetchers -- one per endpoint
// ---------------------------------------------------------------------------

export const api = {
  // -- auth ---------------------------------------------------------------
  login: (body: LoginBody) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  me: () => request<Account>("/auth/me"),

  registerCustomer: (body: CustomerRegisterBody) =>
    request<TokenResponse>("/auth/register/customer", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  registerWorker: (body: WorkerRegisterBody) =>
    request<TokenResponse>("/auth/register/worker", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  registerStaff: (role: "government" | "supplier", body: StaffRegisterBody) =>
    request<TokenResponse>(`/auth/register/${role}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // -- data ---------------------------------------------------------------
  listSites: () => request<Site[]>("/sites"),

  siteSummary: (siteId: string) =>
    request<SiteSummary>(`/sites/${siteId}/summary`),

  siteReadings: (siteId: string, days = 7) =>
    request<Reading[]>(`/sites/${siteId}/readings?days=${days}`),

  siteBills: (siteId: string) => request<Bill[]>(`/sites/${siteId}/bills`),

  listIssues: () => request<Issue[]>("/issues"),

  createIssue: (body: IssueCreate) =>
    request<Issue>("/issues", { method: "POST", body: JSON.stringify(body) }),

  listWorkOrders: () => request<WorkOrder[]>("/work-orders"),

  updateWorkOrderStatus: (orderId: string, status: WorkOrderStatus) =>
    request<WorkOrder>(`/work-orders/${orderId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  pendingAgreements: () => request<Agreement[]>("/agreements/pending"),

  decideAgreement: (agreementId: string, status: AgreementDecision) =>
    request<Agreement>(`/agreements/${agreementId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),

  analyticsByArea: () => request<AreaStats[]>("/analytics/by-area"),
};

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

/**
 * Centralised so an invalidation cannot silently miss a cache entry through a
 * typo'd key -- e.g. creating an issue must invalidate `issues.all()`.
 */
export const queryKeys = {
  me: () => ["auth", "me"] as const,
  sites: () => ["sites"] as const,
  siteSummary: (id: string) => ["sites", id, "summary"] as const,
  siteReadings: (id: string, days: number) =>
    ["sites", id, "readings", days] as const,
  siteBills: (id: string) => ["sites", id, "bills"] as const,
  issues: () => ["issues"] as const,
  workOrders: () => ["work-orders"] as const,
  pendingAgreements: () => ["agreements", "pending"] as const,
  analyticsByArea: () => ["analytics", "by-area"] as const,
};

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/**
 * Parse a decimal string for charting or comparison.
 *
 * Only for values headed somewhere that must be a number. Do not use it to
 * produce text for display -- `formatKwh`/`formatMoney` keep the exact digits.
 */
export function toNumber(value: Decimal | null | undefined): number {
  return value == null ? 0 : Number(value);
}

/** Trim a NUMERIC(12,4) to a readable kWh figure without going via a float. */
export function formatKwh(value: Decimal | null | undefined, dp = 2): string {
  if (value == null) return "—";
  const [whole, frac = ""] = value.split(".");
  return dp === 0 ? whole : `${whole}.${frac.padEnd(dp, "0").slice(0, dp)}`;
}

/** Money, to the 2dp people expect, from the exact string the API sent. */
export function formatMoney(
  value: Decimal | null | undefined,
  currency = "BDT",
): string {
  if (value == null) return "—";
  return `${currency} ${formatKwh(value, 2)}`;
}
