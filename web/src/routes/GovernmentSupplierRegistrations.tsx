import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, queryKeys, type PendingSupplier } from "../lib/api";
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
 * Approving an installer's staff account.
 *
 * This queue replaced a shared registration code — one string, the same for
 * every firm in the city, never rotated. Anyone who had it could attach
 * themselves to any installer on the public list. Nothing on the registration
 * form is treated as evidence any more: the applicant claims a name, a
 * National ID and an organisation, and an official checks all three against
 * records the form cannot reach.
 *
 * Which is why the row shows those three and the firm's licence number, and
 * why there is nothing to open. The decision is a comparison against something
 * outside this system; a detail page would only be a bigger version of the
 * same four facts.
 *
 * Scope is the official's own district, from the server — and it is the
 * district the *applicant registered for*, not everywhere their firm works. A
 * firm covering four districts is four decisions by four officials, so
 * approving someone here never vouches for a colleague next door.
 *
 * Rejection asks for a reason on the row; approval does not ask for anything.
 * Same asymmetry as the worker and agreement queues: approval is the expected
 * outcome, and "rejected" with no reason is not something an applicant can act
 * on.
 */
export default function GovernmentSupplierRegistrations() {
  // Marks this list seen on open and hands back the watermark it replaced, so
  // rows that arrived since the last visit are lit for exactly this render.
  const watermark = useMarkViewSeen(VIEWS.governmentSupplierRegistrations);
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const pending = useQuery({
    queryKey: queryKeys.pendingSupplierRegistrations(),
    queryFn: () => api.pendingSupplierRegistrations(),
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
    }) =>
      api.decideSupplierRegistration(accountId, {
        decision,
        reason: why ?? null,
      }),
    // Invalidate rather than splice the row out: the server maintains
    // approved_at and rejection_reason, and a decision someone else made in
    // the meantime should surface here rather than be papered over.
    onSettled: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.pendingSupplierRegistrations(),
      });
      setRejecting(null);
      setReason("");
    },
  });

  // A 409 means another official decided it first. Worth saying plainly — the
  // refetch above will already have removed the row, so a generic failure
  // message would leave someone wondering what they broke.
  const conflict =
    decide.error instanceof ApiError && decide.error.status === 409;

  return (
    <Card>
      <CardHeader
        title="Supplier approvals"
        subtitle="Installer staff awaiting a decision in your district"
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
          hint="Anyone registering to work for a solar installer in your district appears here. Check their name, National ID and organisation against your records before approving — until you do, they cannot open the supplier portal."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {pending.data.map((row) => (
            <SupplierRow
              key={row.account_id}
              unread={isUnread(row.registered_at, watermark)}
              row={row}
              rejecting={rejecting === row.account_id}
              reason={reason}
              onReason={setReason}
              busy={decide.isPending}
              onApprove={() =>
                decide.mutate({
                  accountId: row.account_id,
                  decision: "approve",
                })
              }
              onStartReject={() => {
                setRejecting(row.account_id);
                setReason("");
              }}
              onCancelReject={() => setRejecting(null)}
              onConfirmReject={() =>
                decide.mutate({
                  accountId: row.account_id,
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

function SupplierRow({
  unread,
  row,
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
  row: PendingSupplier;
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
    (Date.now() - new Date(row.registered_at).getTime()) / 86_400_000,
  );

  return (
    <li className={`px-5 py-4 ${unreadRowClass(unread)}`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="font-medium text-ink">{row.full_name}</p>
          <p className="mt-0.5 text-sm text-ink-muted">
            {row.email}
            {row.phone ? ` · ${row.phone}` : ""}
          </p>
          {/* The three things the decision actually turns on, given their own
              line rather than being folded into the meta row below. */}
          <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <Fact label="National ID" value={row.national_id ?? "—"} mono />
            <Fact label="Organisation" value={row.supplier_name} />
            <Fact
              label="Licence"
              value={row.license_no ?? "None on file"}
              mono={Boolean(row.license_no)}
            />
            <Fact label="Role" value={row.job_title ?? "Not stated"} />
          </dl>
          <p className="mt-2 text-xs text-ink-muted">
            {row.service_district} · registered{" "}
            {waiting === 0
              ? "today"
              : `${waiting} day${waiting === 1 ? "" : "s"} ago`}
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
              placeholder="Not listed as staff by this installer"
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

function Fact({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-2">
      <dt className="shrink-0 text-ink-muted">{label}</dt>
      <dd className={`min-w-0 truncate text-ink-2 ${mono ? "tabular" : ""}`}>
        {value}
      </dd>
    </div>
  );
}
