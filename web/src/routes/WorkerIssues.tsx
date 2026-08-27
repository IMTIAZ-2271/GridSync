import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  queryKeys,
  type Issue,
  type IssueSeverity,
  type TriageStatus,
} from "../lib/api";
import {
  ISSUE_STATUS_TONE,
  NEXT_ISSUE_STATUS,
  SEVERITY_TONE,
  categoryLabel,
  humanize,
} from "../lib/issues";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The triage queue: every issue on a site this worker covers.
 *
 * `GET /api/issues` narrows itself -- `issues_for_worker` returns issues on
 * sites the worker has an assignment against, so this page never filters for
 * visibility, only for attention. That distinction matters: a filter that is
 * doing security work has no business living in the client.
 *
 * Triage is one click. `PATCH /api/issues/{id}/status` accepts any status from
 * any other -- deliberately, because triage gets things wrong and walking a
 * status back must be possible -- so the single button offered per row is
 * convenience, not enforcement, the same posture as the work-order queue and
 * `RequireAuth`.
 *
 * The mutation invalidates rather than patching the cache: acknowledged_at,
 * resolved_at and closed_at are maintained server-side and set once, and the
 * client must not guess at them.
 */

const SEVERITY_RANK: Record<IssueSeverity, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
};

/** Resolution states. Everything else is still someone's problem. */
const SETTLED = new Set(["resolved", "closed", "duplicate"]);

export default function WorkerIssues() {
  const [openOnly, setOpenOnly] = useState(true);
  const queryClient = useQueryClient();

  const advance = useMutation({
    mutationFn: ({ id, status }: { id: string; status: TriageStatus }) =>
      api.updateIssueStatus(id, { status }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.issues() }),
  });

  const issues = useQuery({
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
  });

  const shown = useMemo(() => {
    const all = issues.data ?? [];
    const filtered = openOnly ? all.filter((i) => !SETTLED.has(i.status)) : all;
    // Severity first, then oldest first inside a severity. A critical raised
    // this morning still outranks a low from last week, but among equals the
    // one that has been waiting longest goes to the top -- a queue sorted only
    // by recency quietly buries whatever nobody picked up.
    return [...filtered].sort(
      (a, b) =>
        SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity] ||
        +new Date(a.reported_at) - +new Date(b.reported_at),
    );
  }, [issues.data, openOnly]);

  const openCount = (issues.data ?? []).filter(
    (i) => !SETTLED.has(i.status),
  ).length;

  return (
    <Card>
      <CardHeader
        title="Issue queue"
        subtitle="Reported on the sites you cover. Most severe first, oldest first within a severity."
        action={
          <label className="flex items-center gap-2 text-xs text-ink-2">
            <input
              type="checkbox"
              checked={openOnly}
              onChange={(e) => setOpenOnly(e.target.checked)}
              className="size-3.5 accent-portal-worker"
            />
            Unresolved only
            <span className="tabular text-ink-muted">({openCount})</span>
          </label>
        }
      />

      {issues.isPending ? (
        <div className="space-y-3 p-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-20 w-full" />
          ))}
        </div>
      ) : issues.error ? (
        <ErrorState error={issues.error} />
      ) : shown.length === 0 ? (
        <EmptyState
          title={openOnly ? "Nothing outstanding" : "No issues"}
          hint={
            openOnly
              ? "Every issue on your sites has been resolved or closed."
              : "No issue has been reported on the sites you cover."
          }
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {shown.map((issue) => (
            <IssueRow
              key={issue.issue_id}
              issue={issue}
              busy={advance.isPending && advance.variables?.id === issue.issue_id}
              onAdvance={(status) =>
                advance.mutate({ id: issue.issue_id, status })
              }
            />
          ))}
        </ul>
      )}

      {advance.error && (
        <p className="border-t border-hairline px-5 py-3 text-sm text-status-critical">
          {advance.error.message}
        </p>
      )}

      <p className="border-t border-hairline px-5 py-3 text-xs text-ink-muted">
        The visit itself is tracked on the Work orders tab.
      </p>
    </Card>
  );
}

function IssueRow({
  issue,
  busy,
  onAdvance,
}: {
  issue: Issue;
  busy: boolean;
  onAdvance: (status: TriageStatus) => void;
}) {
  const age = Math.floor(
    (Date.now() - +new Date(issue.reported_at)) / 86_400_000,
  );
  const next = NEXT_ISSUE_STATUS[issue.status];

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-ink">{issue.title}</p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {issue.site_label} · {categoryLabel(issue.category)} ·{" "}
            {issue.reported_by_name} ·{" "}
            {age === 0 ? "today" : age === 1 ? "1 day ago" : `${age} days ago`}
            {/* Phase 1's loop: an issue filed from the equipment page arrives
                already pointing at the device that is wrong, which is what a
                work order gets dispatched against. */}
            {issue.device_id && " · names a device"}
          </p>
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

      {next && (
        <div className="mt-3">
          <button
            type="button"
            disabled={busy}
            onClick={() => onAdvance(next.value)}
            className="rounded-lg border border-hairline px-3 py-1.5 text-sm font-medium text-ink-2 disabled:opacity-50"
          >
            {busy ? "Saving…" : next.label}
          </button>
        </div>
      )}
    </li>
  );
}
