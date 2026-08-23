import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  formatKwh,
  queryKeys,
  type Agreement,
  type AgreementDecision,
} from "../lib/api";
import { humanize } from "../lib/issues";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The approval queue.
 *
 * Approving is what lets a site's exports start earning credit, which is why
 * `PATCH /api/agreements/{id}/status` is government-only -- the utility that
 * pays for the export must not be the party that authorises it. Only two
 * outcomes are reachable from a review: `active` or `terminated`. Suspension
 * is an operational action on an already-live agreement, and nothing returns
 * to `pending`, so neither belongs on this screen.
 *
 * A decision is one click on a queue of strangers' applications, so rejection
 * asks for confirmation on the row itself. Approval does not: it is the
 * expected outcome, and it is reversible by terminating.
 */
export default function GovernmentAgreements() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);

  const pending = useQuery({
    queryKey: queryKeys.pendingAgreements(),
    queryFn: api.pendingAgreements,
  });

  const decide = useMutation({
    mutationFn: ({
      agreementId,
      status,
    }: {
      agreementId: string;
      status: AgreementDecision;
    }) => api.decideAgreement(agreementId, status),
    onSuccess: () => {
      setConfirming(null);
      // The decided row leaves the pending list, and a newly active agreement
      // changes what the by-area rollup will report next time it is read.
      queryClient.invalidateQueries({ queryKey: queryKeys.pendingAgreements() });
      queryClient.invalidateQueries({ queryKey: queryKeys.analyticsByArea() });
    },
  });

  return (
    <Card>
      <CardHeader
        title="Pending net-metering agreements"
        subtitle="Applications waiting on a decision. Oldest first."
        action={
          pending.data ? (
            <Badge tone={pending.data.length > 0 ? "warning" : "good"}>
              {pending.data.length} waiting
            </Badge>
          ) : undefined
        }
      />

      {pending.isPending ? (
        <div className="space-y-3 p-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-28 w-full" />
          ))}
        </div>
      ) : pending.error ? (
        <ErrorState error={pending.error} />
      ) : pending.data.length === 0 ? (
        <EmptyState
          title="Queue is clear"
          hint="Every application has been decided."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {pending.data.map((agreement) => (
            <AgreementRow
              key={agreement.agreement_id}
              agreement={agreement}
              confirming={confirming === agreement.agreement_id}
              onConfirmReject={() => setConfirming(agreement.agreement_id)}
              onCancelReject={() => setConfirming(null)}
              onDecide={(status) =>
                decide.mutate({ agreementId: agreement.agreement_id, status })
              }
              pending={
                decide.isPending &&
                decide.variables?.agreementId === agreement.agreement_id
              }
            />
          ))}
        </ul>
      )}

      {decide.error && (
        <p className="border-t border-hairline px-5 py-3 text-xs text-status-critical">
          {decide.error instanceof ApiError && decide.error.status === 409
            ? // The UPDATE is guarded on status = 'pending', so this means
              // someone else decided it while this page was open. Refetching
              // is the whole fix.
              "Someone else already decided that application. Reloading the queue."
            : `Could not record that decision: ${decide.error.message}`}
        </p>
      )}
    </Card>
  );
}

function AgreementRow({
  agreement,
  confirming,
  onConfirmReject,
  onCancelReject,
  onDecide,
  pending,
}: {
  agreement: Agreement;
  confirming: boolean;
  onConfirmReject: () => void;
  onCancelReject: () => void;
  onDecide: (status: AgreementDecision) => void;
  pending: boolean;
}) {
  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">
            {agreement.account_name}
            <span className="font-normal text-ink-muted">
              {" "}
              · {agreement.site_label}, {agreement.district}
            </span>
          </p>
          <p className="mt-0.5 font-mono text-xs text-ink-muted">
            {agreement.approval_ref} · meter {agreement.billing_device_serial}
          </p>
        </div>

        {confirming ? (
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-xs text-ink-2">Reject this application?</span>
            <button
              type="button"
              disabled={pending}
              onClick={() => onDecide("terminated")}
              className="rounded-md bg-status-critical px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              Yes, reject
            </button>
            <button
              type="button"
              onClick={onCancelReject}
              className="rounded-md border border-hairline px-2.5 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-plane"
            >
              Keep pending
            </button>
          </div>
        ) : (
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => onDecide("active")}
              className="rounded-md bg-portal-government px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              Approve
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={onConfirmReject}
              className="rounded-md border border-hairline px-2.5 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-plane disabled:opacity-40"
            >
              Reject
            </button>
          </div>
        )}
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
        <Figure
          label="Sanctioned capacity"
          value={`${formatKwh(agreement.sanctioned_capacity_kw, 2)} kW`}
        />
        <Figure
          label="Export cap"
          value={`${formatKwh(agreement.export_cap_pct, 0)}%`}
        />
        <Figure
          label="Settlement"
          value={humanize(agreement.settlement_type)}
        />
        <Figure
          label="Credit rollover"
          value={
            agreement.credit_rollover_months
              ? `${agreement.credit_rollover_months} months`
              : "none"
          }
        />
        <Figure
          label="Effective from"
          value={new Date(agreement.effective_from).toLocaleDateString(undefined, {
            day: "numeric",
            month: "short",
            year: "numeric",
          })}
        />
        <Figure
          label="Applied"
          value={new Date(agreement.created_at).toLocaleDateString(undefined, {
            day: "numeric",
            month: "short",
            year: "numeric",
          })}
        />
      </dl>
    </li>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="mt-0.5 font-medium text-ink-2">{value}</dd>
    </div>
  );
}
