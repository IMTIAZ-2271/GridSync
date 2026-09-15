import type { AuditEntry } from "../lib/api";
import { actionLabel, auditChange, formatWhen } from "../lib/admin";

/**
 * Audit entries as a list: what happened, to what, by whom, when, and why.
 * Used by the overview (recent), an account's detail and the audit log page.
 */
export default function AuditList({ entries }: { entries: AuditEntry[] }) {
  return (
    <ul className="divide-y divide-hairline">
      {entries.map((entry) => {
        const { reason, changes } = auditChange(entry);
        return (
          <li key={entry.audit_id} className="px-5 py-3">
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <p className="text-sm font-medium text-ink">{actionLabel(entry.action)}</p>
              <p className="text-xs text-ink-muted">{formatWhen(entry.occurred_at)}</p>
            </div>
            <p className="mt-0.5 text-xs text-ink-muted">
              {entry.actor_email ?? "Command line"}
              {" · "}
              {entry.entity_label ? (
                <span className="text-ink-2">{entry.entity_label}</span>
              ) : (
                <>
                  {entry.entity_type}
                  {entry.entity_id && (
                    <span className="font-mono"> {entry.entity_id.slice(0, 8)}</span>
                  )}
                </>
              )}
              {entry.client_ip && ` · from ${entry.client_ip}`}
            </p>
            {changes.length > 0 && (
              <p className="mt-1 text-xs text-ink-2">
                {changes
                  .map((c) =>
                    c.from === null
                      ? `${c.field}: ${c.to}`
                      : `${c.field}: ${c.from} → ${c.to}`,
                  )
                  .join("; ")}
              </p>
            )}
            {reason && <p className="mt-1 text-xs text-ink-2">“{reason}”</p>}
          </li>
        );
      })}
    </ul>
  );
}
