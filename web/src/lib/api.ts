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
  /** Which of the site's connections this bill was cut for. */
  point_label: string;
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

/**
 * A household's monthly consumption budget, and what it has spent against it.
 *
 * `monthly_kwh` is null when no limit is set -- a normal state, not a missing
 * resource, which is why the endpoint answers 200 with nulls rather than 404.
 * `used_kwh` is always present so the settings form can show current usage
 * before the household has picked a figure, and it is the same month-to-date
 * arithmetic the jobs runner alerts on.
 */
export interface ConsumptionLimit {
  site_id: string;
  month_start: string;
  used_kwh: Decimal;
  monthly_kwh: Decimal | null;
  notify_at_pct: Decimal | null;
  daily_allowance_kwh: Decimal | null;
  updated_at: Timestamp | null;
}

export interface ConsumptionLimitBody {
  monthly_kwh: string;
  notify_at_pct: string;
}

/**
 * A worker registration awaiting a government official's decision.
 *
 * Only government workers reach this queue -- a private installer's staff are
 * approved at registration. Until one is approved they cannot be offered a work
 * order at all (`offerable_worker` refuses a pending profile), so this is a
 * blocking queue, not a formality.
 */
export interface PendingWorker {
  account_id: string;
  full_name: string;
  email: string;
  national_id: string | null;
  employee_code: string;
  service_district: string;
  worker_kind: string;
  availability: string;
  max_daily_jobs: number;
  hired_on: string;
  approval_status: string;
  rejection_reason: string | null;
  approved_at: Timestamp | null;
  distribution_company_id: string | null;
  distribution_company_name: string | null;
  registered_at: Timestamp;
}

export interface WorkerDecisionBody {
  decision: "approve" | "reject";
  reason?: string | null;
}

/**
 * The statuses a triager can set. Narrower than `IssueStatus` on purpose:
 * `duplicate` is tied to `duplicate_of_issue_id` by a CHECK, so the API refuses
 * it and the type refuses it here rather than letting a 409 be the first thing
 * that notices.
 */
export type TriageStatus =
  | "open"
  | "acknowledged"
  | "in_progress"
  | "resolved"
  | "closed";

export interface IssueStatusBody {
  status: TriageStatus;
  resolution_notes?: string | null;
}

export interface CrewMember {
  account_id: string;
  worker_name: string;
  job_role: AssignmentRole;
}

export interface GivenRating {
  stars: number;
  comment: string | null;
  /** Worker ratings only. One exists per visit, whoever else attended. */
  worker_account_id?: string | null;
}

/**
 * A completed visit to one of this household's sites.
 *
 * Carries what the account has already said about it -- verdict and both
 * ratings -- so the page never needs a second request to know which controls
 * are still live.
 */
export interface Visit {
  order_id: string;
  site_id: string;
  site_label: string;
  order_type: WorkOrderType;
  completed_at: Timestamp | null;
  completion_notes: string | null;
  issue_id: string | null;
  issue_title: string | null;
  issue_status: IssueStatus | null;
  consumer_confirmed_at: Timestamp | null;
  consumer_disputed_at: Timestamp | null;
  consumer_feedback: string | null;
  supplier_id: string | null;
  supplier_name: string | null;
  crew: CrewMember[];
  worker_rating: GivenRating | null;
  supplier_rating: GivenRating | null;
}

export interface RatingBody {
  subject: "worker" | "supplier";
  worker_account_id?: string | null;
  stars: number;
  comment?: string | null;
}

export interface VerdictBody {
  resolved: boolean;
  feedback?: string | null;
}

export type ApplicationStatus =
  | "submitted"
  | "under_review"
  | "accepted"
  | "rejected"
  | "withdrawn"
  | "completed";

/**
 * A request to have panels fitted on one connection.
 *
 * Keyed on a billing point, not a site: a household with two connections may
 * fit panels on one and not the other. Some fields are only populated for one
 * side -- contact details of the installer for the household, contact details
 * of the household for the installer.
 */
export interface SolarApplication {
  application_id: string;
  site_id: string;
  site_label: string;
  district: string;
  billing_point_id: string;
  point_label: string;
  supplier_id: string;
  supplier_name: string;
  status: ApplicationStatus;
  requested_capacity_kw: Decimal;
  panel_count: number | null;
  notes: string | null;
  submitted_at: Timestamp;
  decided_at: Timestamp | null;
  decision_notes: string | null;
  installed_array_id: string | null;
  supplier_email?: string | null;
  supplier_phone?: string | null;
  address_line?: string | null;
  account_id?: string | null;
  account_name?: string | null;
  account_phone?: string | null;
  site_has_solar?: boolean | null;
}

export interface ApplicationCreateBody {
  billing_point_id: string;
  supplier_id: string;
  requested_capacity_kw: string;
  panel_count?: number | null;
  notes?: string | null;
}

/**
 * One solar array, judged on what the telemetry actually knows.
 *
 * Per-array, not per-panel: an inverter reports one figure for everything wired
 * to it, so a per-panel verdict would be arithmetic dressed as measurement.
 */
export interface ArrayHealth {
  array_id: string;
  site_id: string;
  label: string | null;
  status: string;
  panel_count: number | null;
  panel_watt_peak: number | null;
  dc_capacity_kw: Decimal;
  azimuth_deg: number | null;
  tilt_deg: number | null;
  shading_factor: Decimal | null;
  commissioned_on: string | null;
  installed_by_supplier_id: string | null;
  installed_by_supplier_name: string | null;
  inverter_device_id: string;
  inverter_serial: string;
  intervals_received: number;
  intervals_expected: number;
  last_reading_at: Timestamp | null;
  generation_kwh: Decimal;
  /** False when one inverter carries several arrays; yield is then null. */
  sole_array_on_inverter: boolean;
  specific_yield_kwh_per_kw: Decimal | null;
}

export interface NetMeteringOutcome {
  district: string;
  site_count: number;
  sites_in_credit: number;
  earned_kwh: Decimal;
  earned_amount: Decimal;
  applied_kwh: Decimal;
  applied_amount: Decimal;
  balance_kwh: Decimal;
  balance_amount: Decimal;
  /** Share of everything earned that has been spent. Null before anything is. */
  applied_pct: Decimal | null;
}

export interface AgreementSummary {
  status: string;
  agreement_count: number;
  sanctioned_capacity_kw: Decimal;
}

export interface NetMeteringReport {
  by_area: NetMeteringOutcome[];
  agreements: AgreementSummary[];
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
  /** The connection this bill was cut for. A site may hold several. */
  billing_point_id: string;
  point_label: string;
  point_reference: string | null;
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

/**
 * Derived telemetry health for one device. Computed in SQL from interval
 * coverage, never read off `device.status` -- see db/sql/dao/device_queries.sql.
 *
 *   healthy   the window is >= 90% covered and the device reported recently
 *   degraded  reporting, but with gaps
 *   silent    nothing in the last 48 hours
 *   no_data   never reported at all
 *   faulty    flagged by hand or by a field visit, whatever the rows say
 *   unknown   installed too recently to have owed a single interval yet
 */
export type DeviceHealth =
  | "healthy"
  | "degraded"
  | "silent"
  | "no_data"
  | "faulty"
  | "unknown";

export type DeviceType = "meter" | "inverter";
export type BillingRole = "billing" | "generation_only" | "check_meter";

export interface SiteDevice {
  device_id: string;
  /** Carried on both scopes, so the fleet and per-site reads share a shape. */
  site_id: string;
  site_label: string;
  district: string;
  device_type: DeviceType;
  serial_no: string;
  manufacturer: string | null;
  model: string | null;
  firmware_version: string | null;
  interval_minutes: number;
  installed_at: Timestamp;
  status: "active" | "faulty" | "removed";

  /** Meter only. Rule 7: exactly one device per site is the billing meter. */
  billing_role: BillingRole | null;
  meter_flow: "unidirectional" | "bidirectional" | null;

  /** Inverter only. */
  ac_capacity_kw: Decimal | null;
  array_count: number;
  dc_capacity_kw: Decimal | null;
  array_status: string | null;

  /** Last 7 whole Asia/Dhaka days, clipped at installed_at, excluding today. */
  window_from: Timestamp;
  window_to: Timestamp;
  last_reading_at: Timestamp | null;
  intervals_expected: number;
  intervals_received: number;
  intervals_suspect: number;
  coverage_pct: Decimal | null;

  health: DeviceHealth;
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
  /** Who the complaint names. Both optional -- most reports name nobody. */
  distribution_company_id: string | null;
  distribution_company_name: string | null;
  supplier_id: string | null;
  supplier_name: string | null;
  /** Supplier inbox only: a complaint about us, not merely one we touch. */
  against_us?: boolean | null;
}

/** A company a complaint from a given site could reasonably be against. */
export interface IssueTarget {
  kind: "distribution" | "supplier";
  id: string;
  name: string;
  /** Attached to this site (its utility, its installer) rather than merely
   *  serving the district. The form preselects one of these. */
  attached: boolean;
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
  /** Consumer requirement 6: which company the complaint names. At most one is
   *  meaningful, and which depends on the category -- see CATEGORY_TARGET. */
  distribution_company_id?: string | null;
  supplier_id?: string | null;
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
  /** Three hours from the offer. Null once answered, expired or declined. */
  offer_expires_at: Timestamp | null;
  /** One day from accepting. Null until accepted. */
  start_deadline_at: Timestamp | null;
}

export interface WorkOrderCreateBody {
  issue_id?: string | null;
  site_id?: string | null;
  device_id?: string | null;
  order_type: WorkOrderType;
  priority?: number | null;
  scheduled_for?: Timestamp | null;
}

/** An unresolved issue with no live work order against it. */
export interface DispatchableIssue {
  issue_id: string;
  site_id: string;
  site_label: string;
  district: string;
  device_id: string | null;
  device_serial: string | null;
  category: IssueCategory;
  severity: IssueSeverity;
  status: IssueStatus;
  title: string;
  description: string | null;
  priority: number;
  reported_at: Timestamp;
  reported_by_name: string;
}

export interface AssignableWorker {
  account_id: string;
  full_name: string;
  employee_code: string;
  service_district: string;
  worker_kind: string;
  availability: string;
  max_daily_jobs: number;
  distribution_company_name: string | null;
  /** Offers plus acceptances outstanding. Capacity already spoken for. */
  open_jobs: number;
  /** Null until service_rating has rows -- never 0, which would read as bad. */
  rating_avg: Decimal | null;
  rating_count: number;
}

export interface AssignmentState {
  order_id: string;
  account_id: string;
  worker_name: string;
  status: AssignmentStatus;
  offer_expires_at: Timestamp | null;
  start_deadline_at: Timestamp | null;
  order_status: WorkOrderStatus;
}

export interface WorkOrder {
  order_id: string;
  site_id: string;
  site_label: string;
  /** The site's district. A job may only be offered to a worker who serves it. */
  district: string;
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

export type WorkerKind = "government" | "private";
export type ApprovalStatus = "pending" | "approved" | "rejected";

/**
 * What a worker's own row says about them, resolved server-side at sign-in.
 *
 * The portal must not ask a worker which kind they are, and must not believe
 * them if it did -- this comes from `worker_profile`. `approval_status` is
 * what gates a government worker's queue until an official in their district
 * approves the registration.
 */
export interface WorkerContext {
  worker_kind: WorkerKind;
  approval_status: ApprovalStatus;
  service_district: string;
  rejection_reason: string | null;
  distribution_company_id: string | null;
  distribution_company_code: string | null;
  distribution_company_name: string | null;
}

export interface Account {
  account_id: string;
  email: string;
  full_name: string;
  phone: string | null;
  role: Role;
  status: string;
  created_at: Timestamp | null;
  national_id: string | null;
  /** Set for role 'worker' only. */
  worker: WorkerContext | null;
  /** Set for role 'supplier' only. */
  supplier_id: string | null;
  supplier_name: string | null;
  /** Set for role 'government' only -- the district their code was issued for. */
  government_district: string | null;
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

/** Every registration collects one. 10, 13 or 17 digits; spaces are fine. */
export interface RegisterBase {
  email: string;
  password: string;
  full_name: string;
  national_id: string;
}

/**
 * No meter serial: a billing meter ID is deliberately NOT collected at
 * sign-up. A household claims an existing connection afterwards with
 * `claimSite`, or builds a new one through the onboarding wizard.
 */
export interface CustomerRegisterBody extends RegisterBase {
  phone?: string | null;
}

export interface WorkerRegisterBody extends RegisterBase {
  phone?: string | null;
  worker_kind: WorkerKind;
  service_district: string;
  /** Required for a government worker, refused for a private one. */
  distribution_company_id?: string | null;
  /**
   * Claims a seeded worker profile instead of creating one, keeping the work
   * orders that profile already holds. A demo affordance, not part of normal
   * registration.
   */
  employee_code?: string;
}

export interface GovernmentRegisterBody extends RegisterBase {
  /** Pre-issued, one per official, single use. e.g. GOV-GULSHAN-01 */
  official_code: string;
}

export interface SupplierRegisterBody extends RegisterBase {
  registration_code: string;
  /** Which installer this person works for, e.g. NOOR. */
  supplier_code: string;
  job_title?: string | null;
}

// ---------------------------------------------------------------------------
// Organisations -- reference data every registration and issue form reads.
// ---------------------------------------------------------------------------

export interface District {
  name: string;
  latitude: Decimal;
  longitude: Decimal;
}

export interface DistributionCompany {
  company_id: string;
  code: string;
  name: string;
  contact_email: string | null;
  contact_phone: string | null;
  districts: string[];
}

export interface SupplierCompany {
  supplier_id: string;
  code: string;
  name: string;
  license_no: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  districts: string[];
  /** Null, not 0, when nobody has rated them -- unrated is not badly rated. */
  rating_avg: Decimal | null;
  rating_count: number;
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export type NotificationSeverity = "info" | "warning" | "critical";

export interface Notification {
  notification_id: number;
  kind: string;
  severity: NotificationSeverity;
  title: string;
  body: string | null;
  /**
   * A loose pointer at whatever this is about -- deliberately not a foreign
   * key server-side, so a notification outlives the row that caused it. Link
   * only the entity_types you recognise; render the rest as plain text.
   */
  entity_type: string | null;
  entity_id: string | null;
  created_at: Timestamp;
  read_at: Timestamp | null;
}

export interface NotificationPage {
  items: Notification[];
  /** Served with the list so the badge and the panel cannot disagree. */
  unread_count: number;
}

export interface ReadResult {
  marked_read: number;
  unread_count: number;
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
// Onboarding -- a customer with no site building one from scratch.
// ---------------------------------------------------------------------------

export type ConnectionType = "residential" | "commercial" | "industrial";

export interface TariffPlan {
  plan_id: string;
  code: string;
  name: string;
  customer_class: ConnectionType;
  currency: string;
  fixed_monthly_charge: Decimal;
  tax_rate: Decimal;
}

export interface SiteCreateBody {
  address_line: string;
  city: string;
  district: string;
  postal_code?: string | null;
  connection_type: ConnectionType;
  sanctioned_load_kw: number;
  tariff_plan_id: string;
}

/**
 * One metering position on a site -- one connection the utility bills.
 *
 * A household may hold several. Rule 7 (exactly one active billing meter) is
 * enforced per point, not per site, so each of these has its own meter, its
 * own bills and its own credit balance.
 */
export interface BillingPoint {
  point_id: string;
  label: string;
  /** The connection number on the utility's paperwork, if it was given. */
  reference: string | null;
  created_at: Timestamp;
  /** Null while the point exists but its meter has not been registered yet. */
  meter_device_id: string | null;
  meter_serial: string | null;
  meter_last_seen_at: Timestamp | null;
  has_solar: boolean;
}

export interface MeterRegisterBody {
  serial_no: string;
  manufacturer: string;
  model: string;
  /** An existing connection to meter. Omit during onboarding. */
  point_id?: string;
  /** Opens a new connection under this name instead. */
  point_label?: string;
  point_reference?: string;
}

export interface MeterRegisterResult {
  device_id: string;
  serial_no: string;
  point_id: string;
  point_label: string;
  point_reference: string | null;
  backfill_from: DateOnly;
  backfill_to: DateOnly;
  readings_backfilled: number;
}

export interface SolarRegisterBody {
  /** Required once the site has more than one billing meter. */
  point_id?: string;
  capacity_kw: number;
  panel_count: number;
  azimuth_deg?: number;
  tilt_deg?: number;
  manufacturer?: string;
  model?: string;
}

export interface SolarRegisterResult {
  inverter_device_id: string;
  array_id: string;
  agreement_id: string;
  point_id: string;
  /** False when this array joined the agreement already covering the point. */
  agreement_created: boolean;
  /** Arrays now on this connection, and their combined AC capacity -- this one included. */
  array_count: number;
  point_capacity_kw: Decimal;
  backfill_from: DateOnly;
  backfill_to: DateOnly;
  readings_backfilled: number;
  /** The meter's own history, re-netted against the connection's TOTAL capacity. */
  meter_readings_updated: number;
}

export interface BillingRunResult {
  billing_point_id: string;
  point_label: string;
  period_start: DateOnly;
  status: "billed" | "skipped";
  bill_id: string | null;
  reason: string | null;
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

  registerGovernment: (body: GovernmentRegisterBody) =>
    request<TokenResponse>("/auth/register/government", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  registerSupplier: (body: SupplierRegisterBody) =>
    request<TokenResponse>("/auth/register/supplier", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // -- data ---------------------------------------------------------------
  listSites: () => request<Site[]>("/sites"),

  // -- notifications --------------------------------------------------------
  listNotifications: (unreadOnly = false) =>
    request<NotificationPage>(
      `/notifications${unreadOnly ? "?unread_only=true" : ""}`,
    ),

  markNotificationRead: (id: number) =>
    request<ReadResult>(`/notifications/${id}/read`, { method: "POST" }),

  markAllNotificationsRead: () =>
    request<ReadResult>("/notifications/read-all", { method: "POST" }),

  // -- organisations --------------------------------------------------------
  listDistricts: () => request<District[]>("/districts"),

  listDistributionCompanies: (district?: string) =>
    request<DistributionCompany[]>(
      `/distribution-companies${district ? `?district=${encodeURIComponent(district)}` : ""}`,
    ),

  listSuppliers: (district?: string) =>
    request<SupplierCompany[]>(
      `/suppliers${district ? `?district=${encodeURIComponent(district)}` : ""}`,
    ),

  // -- onboarding -----------------------------------------------------------
  claimSite: (meterSerial: string) =>
    request<Site>("/sites/claim", {
      method: "POST",
      body: JSON.stringify({ meter_serial: meterSerial }),
    }),

  listBillingPoints: (siteId: string) =>
    request<BillingPoint[]>(`/sites/${siteId}/points`),

  listTariffPlans: (connectionType?: ConnectionType) =>
    request<TariffPlan[]>(
      `/tariff-plans${connectionType ? `?connection_type=${connectionType}` : ""}`,
    ),

  createSite: (body: SiteCreateBody) =>
    request<Site>("/sites", { method: "POST", body: JSON.stringify(body) }),

  registerMeter: (siteId: string, body: MeterRegisterBody) =>
    request<MeterRegisterResult>(`/sites/${siteId}/meter`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  registerSolar: (siteId: string, body: SolarRegisterBody) =>
    request<SolarRegisterResult>(`/sites/${siteId}/solar`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  billSite: (siteId: string) =>
    request<BillingRunResult[]>(`/sites/${siteId}/bill`, { method: "POST" }),

  siteSummary: (siteId: string) =>
    request<SiteSummary>(`/sites/${siteId}/summary`),

  siteReadings: (siteId: string, days = 7) =>
    request<Reading[]>(`/sites/${siteId}/readings?days=${days}`),

  consumptionLimit: (siteId: string) =>
    request<ConsumptionLimit>(`/sites/${siteId}/consumption-limit`),

  setConsumptionLimit: (siteId: string, body: ConsumptionLimitBody) =>
    request<ConsumptionLimit>(`/sites/${siteId}/consumption-limit`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  clearConsumptionLimit: (siteId: string) =>
    request<void>(`/sites/${siteId}/consumption-limit`, { method: "DELETE" }),

  pendingWorkers: () => request<PendingWorker[]>("/workers/pending"),

  decideWorker: (accountId: string, body: WorkerDecisionBody) =>
    request<PendingWorker>(`/workers/${accountId}/approval`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  updateIssueStatus: (issueId: string, body: IssueStatusBody) =>
    request<Issue>(`/issues/${issueId}/status`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  visits: () => request<Visit[]>("/visits"),

  rateVisit: (orderId: string, body: RatingBody) =>
    request<Visit>(`/work-orders/${orderId}/rating`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setIssueVerdict: (issueId: string, body: VerdictBody) =>
    request<{ issue_id: string; resolved: boolean; status: string }>(
      `/issues/${issueId}/verdict`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  solarApplications: (openOnly = false) =>
    request<SolarApplication[]>(
      "/solar-applications" + (openOnly ? "?open_only=true" : ""),
    ),

  createSolarApplication: (body: ApplicationCreateBody) =>
    request<SolarApplication>("/solar-applications", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  decideSolarApplication: (
    id: string,
    status: ApplicationStatus,
    notes?: string,
  ) =>
    request<SolarApplication>(`/solar-applications/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status, notes: notes ?? null }),
    }),

  issueTargets: (siteId: string) =>
    request<IssueTarget[]>(`/sites/${siteId}/issue-targets`),

  siteArrays: (siteId: string) =>
    request<ArrayHealth[]>(`/sites/${siteId}/arrays`),

  netMetering: (district?: string) =>
    request<NetMeteringReport>(
      "/analytics/net-metering" +
        (district ? `?district=${encodeURIComponent(district)}` : ""),
    ),

  siteBills: (siteId: string) => request<Bill[]>(`/sites/${siteId}/bills`),

  siteDevices: (siteId: string) =>
    request<SiteDevice[]>(`/sites/${siteId}/devices`),

  /** Every reporting device in the fleet. Government and supplier only. */
  fleetDevices: () => request<SiteDevice[]>("/devices"),

  listIssues: () => request<Issue[]>("/issues"),

  createIssue: (body: IssueCreate) =>
    request<Issue>("/issues", { method: "POST", body: JSON.stringify(body) }),

  listWorkOrders: () => request<WorkOrder[]>("/work-orders"),

  createWorkOrder: (body: WorkOrderCreateBody) =>
    request<WorkOrder>("/work-orders", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  dispatchableIssues: () =>
    request<DispatchableIssue[]>("/work-orders/dispatchable-issues"),

  assignableWorkers: (district?: string) =>
    request<AssignableWorker[]>(
      "/workers" + (district ? `?district=${encodeURIComponent(district)}` : ""),
    ),

  offerAssignment: (orderId: string, accountId: string, jobRole = "assistant") =>
    request<AssignmentState>(`/work-orders/${orderId}/assignments`, {
      method: "POST",
      body: JSON.stringify({ account_id: accountId, job_role: jobRole }),
    }),

  respondToAssignment: (
    orderId: string,
    decision: "accept" | "decline",
    reason?: string,
  ) =>
    request<AssignmentState>(`/work-orders/${orderId}/assignment`, {
      method: "PATCH",
      body: JSON.stringify({ decision, reason: reason ?? null }),
    }),

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

  analyticsByArea: (district?: string) =>
    request<AreaStats[]>(
      "/analytics/by-area" +
        (district ? `?district=${encodeURIComponent(district)}` : ""),
    ),
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
  districts: () => ["districts"] as const,
  tariffPlans: (connectionType?: ConnectionType) =>
    ["tariff-plans", connectionType ?? "any"] as const,
  siteSummary: (id: string) => ["sites", id, "summary"] as const,
  siteReadings: (id: string, days: number) =>
    ["sites", id, "readings", days] as const,
  siteBills: (id: string) => ["sites", id, "bills"] as const,
  siteDevices: (id: string) => ["sites", id, "devices"] as const,
  siteArrays: (id: string) => ["sites", id, "arrays"] as const,
  netMetering: (district?: string) =>
    ["analytics", "net-metering", district ?? "all"] as const,
  sitePoints: (id: string) => ["sites", id, "points"] as const,
  consumptionLimit: (id: string) =>
    ["sites", id, "consumption-limit"] as const,
  fleetDevices: () => ["devices"] as const,
  issues: () => ["issues"] as const,
  issueTargets: (siteId: string) =>
    ["sites", siteId, "issue-targets"] as const,
  visits: () => ["visits"] as const,
  solarApplications: (openOnly = false) =>
    ["solar-applications", openOnly ? "open" : "all"] as const,
  workOrders: () => ["work-orders"] as const,
  dispatchableIssues: () => ["work-orders", "dispatchable"] as const,
  assignableWorkers: (district?: string) =>
    ["workers", district ?? "all"] as const,
  pendingAgreements: () => ["agreements", "pending"] as const,
  pendingWorkers: () => ["workers", "pending"] as const,
  analyticsByArea: (district?: string) =>
    ["analytics", "by-area", district ?? "all"] as const,
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

/**
 * Exact sum of NUMERIC decimal strings, as a decimal string.
 *
 * `values.reduce((a, b) => a + Number(b), 0)` is the obvious version and it is
 * the thing this module exists to prevent: a total that reaches the screen
 * having passed through a double. These are scaled to integers, summed as
 * BigInt, and formatted back, so the result is exact at `dp` places.
 *
 * Use it for any figure that gets *displayed*. `toNumber` remains correct for
 * a chart axis or a width, where a number is genuinely required.
 */
export function sumDecimals(
  values: (Decimal | null | undefined)[],
  dp = 4,
): Decimal {
  const scale = 10n ** BigInt(dp);
  let total = 0n;
  for (const value of values) {
    if (value == null) continue;
    total += scaleDecimal(value, dp, scale);
  }
  return formatScaled(total, dp, scale);
}

/** Exact `a - b`, same contract as sumDecimals. May return a negative string. */
export function subtractDecimals(
  a: Decimal | null | undefined,
  b: Decimal | null | undefined,
  dp = 4,
): Decimal {
  const scale = 10n ** BigInt(dp);
  return formatScaled(
    scaleDecimal(a ?? "0", dp, scale) - scaleDecimal(b ?? "0", dp, scale),
    dp,
    scale,
  );
}

function scaleDecimal(value: Decimal, dp: number, scale: bigint): bigint {
  const negative = value.startsWith("-");
  const [whole, frac = ""] = (negative ? value.slice(1) : value).split(".");
  // padEnd then slice: pads a short fraction out to dp and truncates a longer
  // one, so "1.5" and "1.50000" scale identically.
  const scaled =
    BigInt(whole || "0") * scale + BigInt(frac.padEnd(dp, "0").slice(0, dp));
  return negative ? -scaled : scaled;
}

function formatScaled(total: bigint, dp: number, scale: bigint): Decimal {
  const negative = total < 0n;
  const abs = negative ? -total : total;
  const frac = (abs % scale).toString().padStart(dp, "0");
  return `${negative ? "-" : ""}${abs / scale}.${frac}`;
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
