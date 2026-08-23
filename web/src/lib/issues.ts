/**
 * Shared issue vocabulary.
 *
 * Two portals read the same `issue` rows from different ends -- a customer
 * files and watches their own, a worker triages every issue on the sites they
 * cover. Both need the same enum labels and the same badge tones, and two
 * copies would eventually disagree about what "medium" looks like or what
 * `data_gap` is called. The enum values themselves are the schema's; only the
 * human labels live here.
 */
import type { IssueCategory, IssueSeverity, IssueStatus } from "./api";

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
