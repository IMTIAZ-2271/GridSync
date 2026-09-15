/**
 * Shared vocabulary for the admin panel, so the overview, the account page and
 * the audit log describe the same row the same way.
 */
import type { AccountStatus, AuditEntry } from "./api";

export const ACCOUNT_STATUS: Record<AccountStatus, { label: string; tone: string }> = {
  active: { label: "Active", tone: "good" },
  suspended: { label: "Suspended", tone: "warning" },
  closed: { label: "Closed", tone: "neutral" },
};

/** What each audit action means, in words. Unknown actions show their key. */
const ACTIONS: Record<string, string> = {
  "admin.bootstrap": "Admin created from the command line",
  "account.status": "Changed account status",
  "account.sessions_revoked": "Signed out everywhere",
  "account.password_reset": "Reset password",
  "account.admin_granted": "Granted admin",
  "account.admin_revoked": "Revoked admin",
};

export function actionLabel(action: string): string {
  return ACTIONS[action] ?? action;
}

export function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

/** The stated reason and any changed fields, parsed from the stored JSON. */
export function auditChange(entry: AuditEntry): {
  reason: string | null;
  changes: { field: string; from: string | null; to: string | null }[];
} {
  const parse = (text: string | null): Record<string, unknown> => {
    if (!text) return {};
    try {
      return JSON.parse(text) as Record<string, unknown>;
    } catch {
      return {};
    }
  };
  const before = parse(entry.before_state);
  const after = parse(entry.after_state);
  const reason = typeof after.reason === "string" ? after.reason : null;
  const fields = new Set([...Object.keys(before), ...Object.keys(after)]);
  fields.delete("reason");
  const show = (v: unknown) => (v === undefined || v === null ? null : String(v));
  return {
    reason,
    changes: [...fields].map((field) => ({
      field,
      from: show(before[field]),
      to: show(after[field]),
    })),
  };
}
