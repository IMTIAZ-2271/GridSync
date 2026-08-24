import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, queryKeys, type Visit } from "../lib/api";
import { ORDER_TYPE_LABEL } from "../lib/workOrders";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Consumer requirement 10, second and third clauses: confirm the fault was
 * really fixed, and rate the people who came.
 *
 * This page answers a promise the system has been making out loud for days --
 * the completion notification already says "please confirm whether the problem
 * is actually resolved", and until now there was nowhere to do it.
 *
 * Two decisions worth knowing when reading this:
 *
 * **A dispute is not a complaint form.** Saying it was not fixed sends the
 * issue back to `in_progress`, which returns it to the worker triage queue and
 * to the dispatcher's inbox. It is a button that causes another visit, so it
 * asks for a reason and says what will happen.
 *
 * **Stars are submitted with their comment, in one action.** The rating cannot
 * be edited afterwards -- it is testimony about a particular visit -- so
 * pressing a star must not be what saves it. Choose, write, then send.
 */
export default function CustomerVisits() {
  const queryClient = useQueryClient();
  const visits = useQuery({
    queryKey: queryKeys.visits(),
    queryFn: api.visits,
  });

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.visits() }),
      // A verdict moves the issue's own status, which the issues page shows.
      queryClient.invalidateQueries({ queryKey: queryKeys.issues() }),
    ]);

  const rate = useMutation({
    mutationFn: ({
      orderId,
      body,
    }: {
      orderId: string;
      body: Parameters<typeof api.rateVisit>[1];
    }) => api.rateVisit(orderId, body),
    onSuccess: refresh,
  });

  const verdict = useMutation({
    mutationFn: ({
      issueId,
      resolved,
      feedback,
    }: {
      issueId: string;
      resolved: boolean;
      feedback?: string;
    }) => api.setIssueVerdict(issueId, { resolved, feedback: feedback ?? null }),
    onSuccess: refresh,
  });

  const error = rate.error ?? verdict.error;

  return (
    <Card>
      <CardHeader
        title="Visits"
        subtitle="Completed work at your sites — tell us how it went"
      />

      {error && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-status-critical">
          {error instanceof ApiError ? String(error.detail) : error.message}
        </p>
      )}

      {visits.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-28 w-full" />
          <Skeleton className="h-28 w-full" />
        </div>
      ) : visits.error ? (
        <ErrorState error={visits.error} />
      ) : visits.data.length === 0 ? (
        <EmptyState
          title="No completed visits yet"
          hint="When a technician finishes a job at one of your sites it appears here, and you can confirm whether it fixed the problem."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {visits.data.map((visit) => (
            <VisitRow
              key={visit.order_id}
              visit={visit}
              busy={
                (rate.isPending && rate.variables?.orderId === visit.order_id) ||
                (verdict.isPending &&
                  verdict.variables?.issueId === visit.issue_id)
              }
              onRate={(body) => rate.mutate({ orderId: visit.order_id, body })}
              onVerdict={(resolved, feedback) =>
                visit.issue_id &&
                verdict.mutate({ issueId: visit.issue_id, resolved, feedback })
              }
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function VisitRow({
  visit,
  busy,
  onRate,
  onVerdict,
}: {
  visit: Visit;
  busy: boolean;
  onRate: (body: Parameters<typeof api.rateVisit>[1]) => void;
  onVerdict: (resolved: boolean, feedback?: string) => void;
}) {
  const [disputing, setDisputing] = useState(false);
  const [reason, setReason] = useState("");

  const answered =
    visit.consumer_confirmed_at !== null || visit.consumer_disputed_at !== null;

  // `rating_one_per_subject` allows ONE worker rating per visit however many
  // people attended -- it is a verdict on the visit, not a scorecard for a
  // crew. So exactly one box is offered, against the lead where there is one,
  // and the label says who else was there rather than pretending the others
  // are separately rateable.
  const rated = visit.worker_rating !== null;
  const ratee =
    visit.crew.find((c) => c.job_role === "lead") ?? visit.crew[0] ?? null;
  const when = visit.completed_at
    ? new Date(visit.completed_at).toLocaleDateString(undefined, {
        dateStyle: "medium",
      })
    : null;

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-ink">
            {ORDER_TYPE_LABEL[visit.order_type]} · {visit.site_label}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {when && `Completed ${when}`}
            {visit.supplier_name && ` · ${visit.supplier_name}`}
            {visit.crew.length > 0 &&
              ` · ${visit.crew.map((c) => c.worker_name).join(", ")}`}
          </p>
          {visit.issue_title && (
            <p className="mt-1 text-sm text-ink-2">
              For: {visit.issue_title}
            </p>
          )}
          {visit.completion_notes && (
            <p className="mt-1 text-sm text-ink-2">{visit.completion_notes}</p>
          )}
        </div>
        {visit.consumer_confirmed_at && <Badge tone="good">confirmed fixed</Badge>}
        {visit.consumer_disputed_at && (
          <Badge tone="critical">reported still broken</Badge>
        )}
      </div>

      {/* --- the verdict ------------------------------------------------ */}
      {visit.issue_id && !answered && (
        <div className="mt-3 rounded-lg border border-hairline p-3">
          <p className="text-sm font-medium text-ink-2">
            Did this fix the problem?
          </p>
          {!disputing ? (
            <div className="mt-2 flex flex-wrap items-center gap-3">
              <button
                type="button"
                disabled={busy}
                onClick={() => onVerdict(true)}
                className="rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
              >
                Yes, it is fixed
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setDisputing(true)}
                className="text-sm text-ink-muted underline disabled:opacity-50"
              >
                No, it is still a problem
              </button>
            </div>
          ) : (
            <div className="mt-2 space-y-2">
              <label className="block text-sm">
                <span className="text-ink-2">What is still wrong?</span>
                <input
                  type="text"
                  autoFocus
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="The meter is still clicking at night"
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
              </label>
              <p className="text-xs text-ink-muted">
                This reopens your report and puts it back in the queue for
                another visit.
              </p>
              <div className="flex items-center gap-3">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onVerdict(false, reason.trim() || undefined)}
                  className="rounded-lg bg-status-critical px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
                >
                  {busy ? "Sending…" : "Reopen my report"}
                </button>
                <button
                  type="button"
                  onClick={() => setDisputing(false)}
                  className="text-sm text-ink-muted underline"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {visit.consumer_feedback && (
        <p className="mt-2 text-sm text-ink-2">
          You said: {visit.consumer_feedback}
        </p>
      )}

      {/* --- the ratings ------------------------------------------------ */}
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {rated ? (
          <RatingBox
            label={
              visit.crew.find(
                (c) => c.account_id === visit.worker_rating?.worker_account_id,
              )?.worker_name ?? "The technician"
            }
            sublabel="technician"
            given={visit.worker_rating}
            busy={busy}
            onSubmit={() => undefined}
          />
        ) : (
          ratee && (
            <RatingBox
              label={ratee.worker_name}
              sublabel={
                visit.crew.length > 1
                  ? `${ratee.job_role} · attended with ${visit.crew.length - 1} other`
                  : ratee.job_role
              }
              given={null}
              busy={busy}
              onSubmit={(stars, comment) =>
                onRate({
                  subject: "worker",
                  worker_account_id: ratee.account_id,
                  stars,
                  comment,
                })
              }
            />
          )
        )}
        {visit.supplier_id && (
          <RatingBox
            label={visit.supplier_name ?? "The supplier"}
            sublabel="company"
            given={visit.supplier_rating}
            busy={busy}
            onSubmit={(stars, comment) =>
              onRate({ subject: "supplier", stars, comment })
            }
          />
        )}
      </div>
    </li>
  );
}

/**
 * Stars plus a comment, submitted together.
 *
 * Pressing a star only selects it -- nothing is written until Send, because a
 * rating cannot be edited once it exists and a misclick that saved itself
 * would be unfair to whoever it landed on.
 */
function RatingBox({
  label,
  sublabel,
  given,
  busy,
  onSubmit,
}: {
  label: string;
  sublabel: string;
  given: { stars: number; comment: string | null } | null;
  busy: boolean;
  onSubmit: (stars: number, comment?: string) => void;
}) {
  const [stars, setStars] = useState(0);
  const [comment, setComment] = useState("");

  if (given) {
    return (
      <div className="rounded-lg border border-hairline p-3">
        <p className="text-sm font-medium text-ink">{label}</p>
        <p className="mt-1 text-sm text-ink-2">
          <span aria-hidden>{"★".repeat(given.stars)}</span>
          <span className="sr-only">{given.stars} out of 5</span>
          <span className="ml-2 text-xs text-ink-muted">
            you rated {given.stars}/5
          </span>
        </p>
        {given.comment && (
          <p className="mt-1 text-sm text-ink-2">{given.comment}</p>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-hairline p-3">
      <p className="text-sm font-medium text-ink">{label}</p>
      <p className="text-xs text-ink-muted">{sublabel}</p>

      <div className="mt-2 flex items-center gap-1" role="group"
           aria-label={`Rate ${label} out of 5`}>
        {[1, 2, 3, 4, 5].map((n) => (
          <button
            key={n}
            type="button"
            aria-pressed={stars === n}
            aria-label={`${n} star${n === 1 ? "" : "s"}`}
            onClick={() => setStars(n)}
            className={`text-lg leading-none transition-colors ${
              n <= stars ? "text-status-warning" : "text-hairline"
            }`}
          >
            ★
          </button>
        ))}
        {/* Never colour alone: the chosen value is spelled out. */}
        <span className="ml-2 text-xs text-ink-muted">
          {stars > 0 ? `${stars} of 5` : "not rated"}
        </span>
      </div>

      <input
        type="text"
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="Anything you want to add (optional)"
        className="mt-2 w-full rounded-lg border border-hairline bg-surface px-3 py-1.5 text-sm text-ink"
      />

      <button
        type="button"
        disabled={busy || stars === 0}
        onClick={() => onSubmit(stars, comment.trim() || undefined)}
        className="mt-2 rounded-lg border border-hairline px-3 py-1.5 text-sm font-medium text-ink-2 disabled:opacity-50"
      >
        {busy ? "Sending…" : "Send rating"}
      </button>
      <p className="mt-1 text-xs text-ink-muted">Ratings cannot be changed later.</p>
    </div>
  );
}
