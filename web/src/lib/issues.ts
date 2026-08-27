/**
 * Shared issue vocabulary.
 *
 * Two portals read the same `issue` rows from different ends -- a consumer
 * files and watches their own, a worker triages every issue on the sites they
 * cover. Both need the same enum labels and the same badge tones, and two
 * copies would eventually disagree about what "medium" looks like or what
 * `data_gap` is called. The enum values themselves are the schema's; only the
 * human labels live here.
 */
import type {
  IssueCategory,
  IssueSeverity,
  IssueStatus,
  TriageStatus,
} from "./api";

export const CATEGORIES: { value: IssueCategory; label: string }[] = [
  { value: "billing_dispute", label: "Billing dispute" },
  { value: "export_not_credited", label: "Export not credited" },
  { value: "meter_fault", label: "Meter fault" },
  { value: "inverter_fault", label: "Inverter fault" },
  { value: "outage", label: "Outage" },
  { value: "data_gap", label: "Missing readings" },
  { value: "other", label: "Something else" },
];

export const SEVERITIES: { value: IssueSeverity; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "critical", label: "Critical" },
];

/**
 * Badge tones. Severity climbs; status is about resolution, not urgency, so
 * only `open` and `resolved` carry a tone at all -- the rest are neutral
 * because an acknowledged issue is not better or worse than one in progress.
 */
export const SEVERITY_TONE: Record<IssueSeverity, string> = {
  low: "neutral",
  medium: "warning",
  high: "serious",
  critical: "critical",
};

export const ISSUE_STATUS_TONE: Record<IssueStatus, string> = {
  open: "warning",
  acknowledged: "neutral",
  in_progress: "neutral",
  resolved: "good",
  closed: "neutral",
  duplicate: "neutral",
};

export function categoryLabel(category: IssueCategory | string): string {
  return CATEGORIES.find((c) => c.value === category)?.label ?? category;
}

/** Enum values reach the screen as `in_progress`; nobody reads underscores. */
export function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

/**
 * The move a triager is offered next, per status.
 *
 * Convenience, not enforcement -- `PATCH /api/issues/{id}/status` accepts any
 * of the five from any other, deliberately, because triage gets things wrong
 * and walking a status back must be possible. Offering one obvious next step
 * keeps the common path to a single click without pretending the server has a
 * state machine it does not have. Same posture as `web/src/lib/workOrders.ts`.
 *
 * `closed` and `duplicate` offer nothing: a closed issue is finished, and a
 * duplicate's status follows the issue it was merged into (the API refuses it
 * with a 409).
 */
export const NEXT_ISSUE_STATUS: Record<
  IssueStatus,
  { value: TriageStatus; label: string } | null
> = {
  open: { value: "acknowledged", label: "Acknowledge" },
  acknowledged: { value: "in_progress", label: "Start work" },
  in_progress: { value: "resolved", label: "Mark resolved" },
  resolved: { value: "closed", label: "Close" },
  closed: null,
  duplicate: null,
};

/**
 * Which company a complaint of this kind is against.
 *
 * Consumer requirement 6: a meter fault is the distribution company's, a bad
 * installation is the installer's. Some categories name nobody -- a data gap is
 * not anyone's fault until someone looks, and demanding a culprit on every
 * report would only produce wrong ones.
 *
 * A default, not a rule: the API accepts either field on any category, because
 * a billing dispute about an uncredited export can legitimately involve both
 * parties and refusing that would be inventing a constraint the schema does not
 * have.
 */
export const CATEGORY_TARGET: Record<
  string,
  "distribution" | "supplier" | null
> = {
  meter_fault: "distribution",
  outage: "distribution",
  billing_dispute: "distribution",
  export_not_credited: "distribution",
  net_metering: "distribution",
  inverter_fault: "supplier",
  solar_installation: "supplier",
  supplier_service: "supplier",
  data_gap: null,
  other: null,
};

export const TARGET_LABEL: Record<"distribution" | "supplier", string> = {
  distribution: "Which utility is this about?",
  supplier: "Which installer is this about?",
};
