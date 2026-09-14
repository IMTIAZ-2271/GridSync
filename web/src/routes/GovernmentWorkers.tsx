import { useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, queryKeys, type PendingWorker } from "../lib/api";
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
 * Government requirement 3: the worker approval queue.
 *
 * **Both kinds of worker land here.** A private worker used to be approved by
 * the act of registering -- anyone who filled the form in could be sent to a
 * household's meter -- so they are decided here too, on the same evidence: a
 * name, a National ID, a region, and for a government worker the utility that
 * employs them. The row says which kind it is, because a private worker has no
 * employer to name and a blank where a company would be reads as missing data.
 *
 * The queue blocks real work rather than paperwork. An undecided registration
 * cannot open the worker portal at all, and `assignable_workers` will not offer
 * it a job.
 *
 * Its twin is /government/supplier-registrations, which decides an installer's
 * staff accounts. Same scope rule, same guards, different evidence.
 *
 * Scope is the official's own district and comes from the server, not from a
 * filter here: the API reads it off `government_profile` and a worker outside it
 * answers 404. There is deliberately no district selector on this page -- an
 * official governs one district, and offering a picker would imply otherwise.
 *
 * Newest-first, which the query does -- the burying problem a recency sort
 * creates is answered by the unread indicator rather than by the order.
 *
 * Rejection asks for a reason on the row; approval does not ask for anything.
 * The asymmetry is the same one on the agreements queue: approval is the
 * expected outcome, and a rejection somebody cannot act on is worse than no
 * answer -- "rejected" alone does not tell an applicant what to fix.
 */
export default function GovernmentWorkers() {
  // An admin sees every district's queue (the API is unscoped for them).
  const { account } = useAuth();
  // Marks this list seen on open and hands back the watermark it
  // replaced, so rows that arrived since the last visit are lit for
  // exactly this render and normal on the next load.
  const watermark = useMarkViewSeen(VIEWS.governmentWorkers);
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const pending = useQuery({
    queryKey: queryKeys.pendingWorkers(),
    queryFn: () => api.pendingWorkers(),
  });

  const decide = useMutation({
    mutationFn: ({
      accountId,
      decision,
      why,
    }: {
      accountId: string;
      decision: "approve" | "reject";
      why?: string;
    }) => api.decideWorker(accountId, { decision, reason: why ?? null }),
    // Invalidate rather than splice the row out: the server maintains
    // approved_at and rejection_reason, and a decision someone else made in the
    // meantime should surface here rather than be papered over.
    onSettled: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.pendingWorkers(),
      });
      setRejecting(null);
      setReason("");
    },
  });

  // A 409 means another official decided it first. That is worth saying
  // plainly -- the refetch above will already have removed the row, so a
  // generic failure message would leave someone wondering what they broke.
  const conflict =
    decide.error instanceof ApiError && decide.error.status === 409;

  return (
    <Card>
      <CardHeader
        title="Worker approvals"
        subtitle={`Field workers awaiting a decision ${account?.role === "admin" ? "in every district" : "in your district"}`}
        action={
          pending.data ? (
            <Badge tone={pending.data.length > 0 ? "warning" : "good"}>
              {pending.data.length} pending
            </Badge>
          ) : undefined
        }
      />

      {conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-ink-2">
          Another official decided that registration first. The queue has been
          refreshed.
        </p>
      )}
      {decide.error && !conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-status-critical">
          {decide.error.message}
        </p>
      )}

      {pending.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : pending.error ? (
        <ErrorState error={pending.error} />
      ) : pending.data.length === 0 ? (
        <EmptyState
          title="Nothing waiting"
          hint="Anyone registering as a field worker in your district appears here until you approve or reject them. Until you do, they cannot be offered any work."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {pending.data.map((worker) => (
            <WorkerRow
              unread={isUnread(worker.registered_at, watermark)}
              key={worker.account_id}
              worker={worker}
              rejecting={rejecting === worker.account_id}
              reason={reason}
              onReason={setReason}
              busy={decide.isPending}
              onApprove={() =>
                decide.mutate({
                  accountId: worker.account_id,
                  decision: "approve",
                })
              }
              onStartReject={() => {
                setRejecting(worker.account_id);
                setReason("");
              }}
              onCancelReject={() => setRejecting(null)}
              onConfirmReject={() =>
                decide.mutate({
                  accountId: worker.account_id,
                  decision: "reject",
                  why: reason.trim() || undefined,
                })
              }
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function WorkerRow({
  unread,
  worker,
  rejecting,
  reason,
  onReason,
  busy,
  onApprove,
  onStartReject,
  onCancelReject,
  onConfirmReject,
}: {
  /** Arrived since this account last opened the list. */
  unread: boolean;
  worker: PendingWorker;
  rejecting: boolean;
  reason: string;
  onReason: (v: string) => void;
  busy: boolean;
  onApprove: () => void;
  onStartReject: () => void;
  onCancelReject: () => void;
  onConfirmReject: () => void;
}) {
  const waiting = Math.floor(
    (Date.now() - new Date(worker.registered_at).getTime()) / 86_400_000,
  );

  return (
    <li className={`px-5 py-4 ${unreadRowClass(unread)}`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="font-medium text-ink">{worker.full_name}</p>
          <p className="mt-0.5 text-sm text-ink-muted">
            {worker.email} · <span className="tabular">{worker.employee_code}</span>
          </p>
          {/* The decision is a check against records outside this system, and
              the National ID is what that check is made on. */}
          <p className="mt-1 text-sm text-ink-2">
            National ID{" "}
            <span className="tabular">{worker.national_id ?? "—"}</span>
          </p>
          <p className="mt-1 text-xs text-ink-muted">
            {worker.worker_kind === "government"
              ? (worker.distribution_company_name ??
                "Government worker, no utility recorded")
              : "Private worker"}{" "}
            · {worker.service_district} · registered{" "}
            {waiting === 0 ? "today" : `${waiting} day${waiting === 1 ? "" : "s"} ago`}
          </p>
        </div>

        {!rejecting ? (
          <div className="flex shrink-0 items-center gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={onStartReject}
              className="text-sm text-ink-muted underline disabled:opacity-50"
            >
              Reject
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={onApprove}
              className="rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              Approve
            </button>
          </div>
        ) : null}
      </div>

      {rejecting && (
        <div className="mt-3 space-y-2 rounded-lg border border-hairline p-3">
          <label className="block text-sm">
            <span className="font-medium text-ink-2">
              Why is this being rejected?
            </span>
            <input
              type="text"
              autoFocus
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              placeholder="Employee code does not match our records"
              className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
            />
          </label>
          <p className="text-xs text-ink-muted">
            This is sent to the applicant.
          </p>
          <div className="flex items-center gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={onConfirmReject}
              className="rounded-lg bg-status-critical px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy ? "Rejecting…" : "Confirm rejection"}
            </button>
            <button
              type="button"
              onClick={onCancelReject}
              className="text-sm text-ink-muted underline"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
