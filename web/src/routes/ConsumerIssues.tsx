import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  queryKeys,
  type Issue,
  type IssueCategory,
  type IssueSeverity,
} from "../lib/api";
import { useSelectedSite } from "../components/SitePicker";
// The enum labels and badge tones are shared with the worker portal's triage
// queue -- see web/src/lib/issues.ts. Two copies would eventually disagree
// about what `data_gap` is called.
import {
  CATEGORIES,
  CATEGORY_TARGET,
  TARGET_LABEL,
  ISSUE_STATUS_TONE,
  SEVERITIES,
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

export default function ConsumerIssues() {
  // Marks this list seen on open and hands back the watermark it
  // replaced, so rows that arrived since the last visit are lit for
  // exactly this render and normal on the next load.
  const watermark = useMarkViewSeen(VIEWS.consumerIssues);
  const { siteId, site } = useSelectedSite();
  const queryClient = useQueryClient();

  // The equipment page links here with the device already chosen and a
  // category that matches the symptom it saw. issue.device_id has existed on
  // the API since auth landed and nothing ever set it -- an issue filed from
  // a device card is the first one that arrives already pointing at the thing
  // that is wrong, which is what a work order needs to be dispatched against.
  const [params] = useSearchParams();
  const prefillDeviceId = params.get("device");
  const prefillCategory = params.get("category");

  const issues = useQuery({
    queryKey: queryKeys.issues(),
    queryFn: api.listIssues,
  });

  // /api/issues is scoped server-side now: a consumer's token returns only
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
              <IssueRow
                key={issue.issue_id}
                issue={issue}
                unread={isUnread(issue.reported_at, watermark)}
              />
            ))}
          </ul>
        )}
      </Card>

      <IssueForm
        siteId={siteId}
        deviceId={prefillDeviceId}
        initialCategory={
          CATEGORIES.some((c) => c.value === prefillCategory)
            ? (prefillCategory as IssueCategory)
            : undefined
        }
        onCreated={() =>
          queryClient.invalidateQueries({ queryKey: queryKeys.issues() })
        }
      />
    </div>
  );
}

function IssueRow({
  unread, issue }: {
  /** Arrived since this account last opened the list. */
  unread: boolean; issue: Issue }) {
  const category = categoryLabel(issue.category);

  return (
    <li
      className={`border-b border-hairline px-5 py-4 last:border-0 ${unreadRowClass(unread)}`}
    >
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

const FIELD =
  "w-full rounded-md border border-hairline bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-series-import focus:ring-2 focus:ring-series-import/25";

function IssueForm({
  siteId,
  deviceId,
  initialCategory,
  onCreated,
}: {
  siteId: string | null;
  /** Set when this form was opened from a device card. */
  deviceId?: string | null;
  initialCategory?: IssueCategory;
  onCreated: () => void;
}) {
  const [category, setCategory] = useState<IssueCategory>(
    initialCategory ?? "billing_dispute",
  );
  const [severity, setSeverity] = useState<IssueSeverity>("medium");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [done, setDone] = useState(false);
  // Consumer requirement 6. Which company the dropdown names depends on the
  // category: a meter fault is the utility's, a bad installation the
  // installer's. Some categories name nobody, and the field disappears rather
  // than offering an irrelevant list.
  const [target, setTarget] = useState("");

  const targetKind = CATEGORY_TARGET[category] ?? null;
  const targets = useQuery({
    queryKey: queryKeys.issueTargets(siteId!),
    queryFn: () => api.issueTargets(siteId!),
    enabled: !!siteId,
  });
  const choices = (targets.data ?? []).filter((t) => t.kind === targetKind);

  // Preselect the company actually attached to this site -- its own utility,
  // or the firm that fitted its panels -- rather than making someone pick from
  // a list they have no way to rank. Re-runs when the category changes, since
  // that changes which list is on screen.
  useEffect(() => {
    if (!targetKind) {
      setTarget("");
      return;
    }
    const attached = choices.find((t) => t.attached);
    setTarget(attached?.id ?? (choices.length === 1 ? choices[0].id : ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [targetKind, targets.data]);

  const create = useMutation({
    mutationFn: () =>
      api.createIssue({
        site_id: siteId!,
        category,
        severity,
        title: title.trim(),
        description: description.trim() || null,
        device_id: deviceId ?? null,
        distribution_company_id:
          targetKind === "distribution" && target ? target : null,
        supplier_id: targetKind === "supplier" && target ? target : null,
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

        {targetKind && (
          <div>
            <label htmlFor="issue-target" className="text-xs font-medium text-ink-2">
              {TARGET_LABEL[targetKind]}
            </label>
            <select
              id="issue-target"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className={`mt-1 ${FIELD}`}
            >
              <option value="">
                {targets.isPending
                  ? "Loading…"
                  : choices.length === 0
                    ? "Nobody on record for your area"
                    : "Not sure / leave blank"}
              </option>
              {choices.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                  {t.attached ? " — yours" : ""}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-ink-muted">
              {targetKind === "distribution"
                ? "The company that owns your meter and issues your bill."
                : "The company that fitted your panels."}{" "}
              Leave it blank if you are not sure — it can be worked out later.
            </p>
          </div>
        )}

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
        {/* The server attributes this to the signed-in account; the form
            never carries a reporter. Say so rather than implying otherwise. */}
        <p className="text-xs text-ink-muted">
          {deviceId
            ? "Filed against this device, in your name."
            : "Filed against this site, in your name."}
        </p>
      </form>
    </Card>
  );
}
