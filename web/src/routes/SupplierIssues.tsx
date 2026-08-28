import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api, queryKeys, type Issue, type IssueSeverity } from "../lib/api";
import {
  ISSUE_STATUS_TONE,
  SEVERITY_TONE,
  categoryLabel,
  humanize,
} from "../lib/issues";
import {
  VIEWS,
  isUnread,
  unreadRowClass,
  useMarkViewSeen,
} from "../lib/unread";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Supplier requirement 2: the installer's complaint inbox.
 *
 * A supplier is a fleet-wide reader for most things, but an inbox is not a
 * fleet. `issues_for_supplier` narrows to complaints **named against this firm**
 * — consumer requirement 6's dropdown is what writes that — plus the sites the
 * firm actually works on, and flags which is which.
 *
 * That distinction is the page. A complaint that names you is about your own
 * work and somebody is waiting on your answer. A complaint on a site where you
 * happen to have fitted an array is context: worth seeing, not yours to answer.
 * Mixing them into one list by recency would bury the first kind in the second.
 *
 * Read-only, deliberately. A supplier changes the world through a work order,
 * and `/supplier/dispatch` is where a complaint becomes a visit — the button
 * here points there rather than duplicating it.
 */

const SEVERITY_RANK: Record<IssueSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

const SETTLED = new Set(["resolved", "closed", "duplicate"]);

export default function SupplierIssues() {
  // Marks this list seen on open and hands back the watermark it
  // replaced, so rows that arrived since the last visit are lit for
  // exactly this render and normal on the next load.
  const watermark = useMarkViewSeen(VIEWS.supplierIssues);
  const [openOnly, setOpenOnly] = useState(true);
  const [mineOnly, setMineOnly] = useState(false);

  const issues = useQuery({
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
  });

  const { shown, againstUs, unresolved } = useMemo(() => {
    const all = issues.data ?? [];
    const filtered = all.filter(
      (i) =>
        (!openOnly || !SETTLED.has(i.status)) && (!mineOnly || i.against_us),
    );
    // Ours first, then severity, then oldest -- a queue sorted only by recency
    // buries whoever nobody answered.
    filtered.sort(
      (a, b) =>
        Number(!!b.against_us) - Number(!!a.against_us) ||
        SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
        +new Date(a.reported_at) - +new Date(b.reported_at),
    );
    return {
      shown: filtered,
      againstUs: all.filter((i) => i.against_us && !SETTLED.has(i.status)).length,
      unresolved: all.filter((i) => !SETTLED.has(i.status)).length,
    };
  }, [issues.data, openOnly, mineOnly]);

  return (
    <Card>
      <CardHeader
        title="Complaints"
        subtitle="Named against your firm, and on the sites you work"
        action={
          issues.data ? (
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-xs text-ink-2">
                <input
                  type="checkbox"
                  checked={mineOnly}
                  onChange={(e) => setMineOnly(e.target.checked)}
                />
                About us only
              </label>
              <label className="flex items-center gap-2 text-xs text-ink-2">
                <input
                  type="checkbox"
                  checked={openOnly}
                  onChange={(e) => setOpenOnly(e.target.checked)}
                />
                Unresolved only
              </label>
              <Badge tone={againstUs > 0 ? "warning" : "good"}>
                {againstUs} about us
              </Badge>
            </div>
          ) : undefined
        }
      />

      {issues.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : issues.error ? (
        <ErrorState error={issues.error} />
      ) : shown.length === 0 ? (
        <EmptyState
          title={mineOnly ? "Nothing about your firm" : "Nothing here"}
          hint="Complaints reach you when a household names your firm on the report, or when they are filed on a site you have fitted or visited."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {shown.map((issue) => (
            <IssueRow
              key={issue.issue_id}
              issue={issue}
              unread={isUnread(issue.reported_at, watermark)}
            />
          ))}
        </ul>
      )}

      <p className="border-t border-hairline px-5 py-3 text-xs text-ink-muted">
        {unresolved} unresolved in view. Raising a visit against one of these is
        done on the{" "}
        <Link to="/supplier/dispatch" className="font-medium text-ink-2 underline">
          Dispatch
        </Link>{" "}
        page, where the technician gets picked at the same time.
      </p>
    </Card>
  );
}

function IssueRow({
  unread, issue }: {
  /** Arrived since this account last opened the list. */
  unread: boolean; issue: Issue }) {
  const age = Math.floor(
    (Date.now() - +new Date(issue.reported_at)) / 86_400_000,
  );

  return (
    <li className={`px-5 py-4 ${unreadRowClass(unread)}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 font-medium text-ink">
            {issue.title}
            {issue.against_us && <Badge tone="serious">about your firm</Badge>}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {issue.site_label} · {categoryLabel(issue.category)} ·{" "}
            {issue.reported_by_name} ·{" "}
            {age === 0 ? "today" : `${age} day${age === 1 ? "" : "s"} ago`}
            {issue.device_id && " · names a device"}
          </p>
          {/* When a household named the utility rather than us, say so: it is
              the difference between a complaint we should answer and one we are
              simply near. */}
          {issue.distribution_company_name && (
            <p className="mt-0.5 text-xs text-ink-muted">
              Filed against {issue.distribution_company_name}
            </p>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          <Badge tone={SEVERITY_TONE[issue.severity]}>{issue.severity}</Badge>
          <Badge tone={ISSUE_STATUS_TONE[issue.status]}>
            {humanize(issue.status)}
          </Badge>
        </div>
      </div>

      {issue.description && (
        <p className="mt-2 text-sm text-ink-2">{issue.description}</p>
      )}
    </li>
  );
}
