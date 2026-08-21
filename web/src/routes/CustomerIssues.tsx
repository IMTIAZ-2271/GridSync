import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  queryKeys,
  type Issue,
  type IssueCategory,
  type IssueSeverity,
} from "../lib/api";
import { useSelectedSite } from "../components/SitePicker";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

const CATEGORIES: { value: IssueCategory; label: string }[] = [
  { value: "billing_dispute", label: "Billing dispute" },
  { value: "export_not_credited", label: "Export not credited" },
  { value: "meter_fault", label: "Meter fault" },
  { value: "inverter_fault", label: "Inverter fault" },
  { value: "outage", label: "Outage" },
  { value: "data_gap", label: "Missing readings" },
  { value: "other", label: "Something else" },
];

const SEVERITIES: { value: IssueSeverity; label: string }[] = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "critical", label: "Critical" },
];

const SEVERITY_TONE: Record<IssueSeverity, string> = {
  low: "neutral",
  medium: "warning",
  high: "serious",
  critical: "critical",
};

const STATUS_TONE: Record<string, string> = {
  open: "warning",
  acknowledged: "neutral",
  in_progress: "neutral",
  resolved: "good",
  closed: "neutral",
  duplicate: "neutral",
};

export default function CustomerIssues() {
  const { siteId, site } = useSelectedSite();
  const queryClient = useQueryClient();

  const issues = useQuery({
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
  });

  // /api/issues is scoped server-side now: a customer's token returns only
  // issues on sites they own. The client filter that used to be here is gone
  // -- it was never a boundary, and keeping it would imply it was.
  const mine = issues.data?.filter((i) => i.site_id === siteId) ?? [];

  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_360px] lg:items-start">
      <Card>
        <CardHeader
          title="Your issues"
          subtitle={site ? `Reported for ${site.label}` : undefined}
        />

        {issues.isPending ? (
          <div className="space-y-3 p-5">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        ) : issues.error ? (
          <ErrorState error={issues.error} />
        ) : mine.length === 0 ? (
          <EmptyState
            title="Nothing reported"
            hint="If something looks wrong with your meter, your generation or a bill, file it using the form."
          />
        ) : (
          <ul>
            {mine.map((issue) => (
              <IssueRow key={issue.issue_id} issue={issue} />
            ))}
          </ul>
        )}
      </Card>

      <IssueForm
        siteId={siteId}
        onCreated={() =>
          queryClient.invalidateQueries({ queryKey: queryKeys.issues() })
        }
      />
    </div>
  );
}

function IssueRow({ issue }: { issue: Issue }) {
  const category =
    CATEGORIES.find((c) => c.value === issue.category)?.label ?? issue.category;

  return (
    <li className="border-b border-hairline px-5 py-4 last:border-0">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-ink">{issue.title}</p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {category} · reported{" "}
            {new Date(issue.reported_at).toLocaleDateString(undefined, {
              day: "numeric",
              month: "short",
              year: "numeric",
            })}
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Badge tone={SEVERITY_TONE[issue.severity]}>{issue.severity}</Badge>
          <Badge tone={STATUS_TONE[issue.status]}>
            {issue.status.replace(/_/g, " ")}
          </Badge>
        </div>
      </div>
      {issue.description && (
        <p className="mt-2 text-sm text-ink-2">{issue.description}</p>
      )}
    </li>
  );
}

const FIELD =
  "w-full rounded-md border border-hairline bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-series-import focus:ring-2 focus:ring-series-import/25";

function IssueForm({
  siteId,
  onCreated,
}: {
  siteId: string | null;
  onCreated: () => void;
}) {
  const [category, setCategory] = useState<IssueCategory>("billing_dispute");
  const [severity, setSeverity] = useState<IssueSeverity>("medium");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [done, setDone] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.createIssue({
        site_id: siteId!,
        category,
        severity,
        title: title.trim(),
        description: description.trim() || null,
      }),
    onSuccess: () => {
      setTitle("");
      setDescription("");
      setDone(true);
      onCreated();
    },
  });

  const canSubmit = !!siteId && title.trim().length > 0 && !create.isPending;

  return (
    <Card>
      <CardHeader title="Report an issue" subtitle="Goes to the support queue" />
      <form
        className="space-y-4 p-5"
        onSubmit={(e) => {
          e.preventDefault();
          setDone(false);
          if (canSubmit) create.mutate();
        }}
      >
        <div>
          <label htmlFor="issue-title" className="text-xs font-medium text-ink-2">
            Summary
          </label>
          <input
            id="issue-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={200}
            placeholder="Export credit missing for July"
            className={`mt-1 ${FIELD}`}
          />
        </div>

        <div>
          <label htmlFor="issue-category" className="text-xs font-medium text-ink-2">
            What kind of problem?
          </label>
          <select
            id="issue-category"
            value={category}
            onChange={(e) => setCategory(e.target.value as IssueCategory)}
            className={`mt-1 ${FIELD}`}
          >
            {CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="issue-severity" className="text-xs font-medium text-ink-2">
            How urgent?
          </label>
          <select
            id="issue-severity"
            value={severity}
            onChange={(e) => setSeverity(e.target.value as IssueSeverity)}
            className={`mt-1 ${FIELD}`}
          >
            {SEVERITIES.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="issue-body" className="text-xs font-medium text-ink-2">
            Details <span className="text-ink-muted">(optional)</span>
          </label>
          <textarea
            id="issue-body"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            placeholder="What you expected, and what you saw instead."
            className={`mt-1 resize-y ${FIELD}`}
          />
        </div>

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full rounded-md bg-series-import px-4 py-2 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {create.isPending ? "Filing…" : "File issue"}
        </button>

        {create.error && (
          <p className="text-xs text-status-critical">
            Could not file this: {create.error.message}
          </p>
        )}
        {done && !create.isPending && !create.error && (
          <p className="text-xs font-medium text-status-good-text">
            Filed. It now appears in your list.
          </p>
        )}
        {/* No auth: the server attributes this to the site's owner. Say so
            rather than implying the reader is signed in as someone. */}
        <p className="text-xs text-ink-muted">
          Filed against this site, in your name.
        </p>
      </form>
    </Card>
  );
}
