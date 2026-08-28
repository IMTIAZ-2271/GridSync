import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  formatKwh,
  queryKeys,
  type Agreement,
  type AgreementDecision,
  type WorkOrder,
} from "../lib/api";
import DispatchVisit from "../components/DispatchVisit";
import { netMeteringStage } from "../lib/fulfilment";
import { humanize } from "../lib/issues";
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
 * The approval queue.
 *
 * Approving is what lets a site's exports start earning credit, which is why
 * this is government-only -- the utility that pays for the export must not be
 * the party that authorises it.
 *
 * **Approving is no longer one click.** Since migration b7d3f5a92c14 the
 * regulator orders an inspection first: a technician checks the array and
 * swaps the connection's meter for a bidirectional one, the household confirms
 * it, and only then is the meter registered and the agreement activated. An
 * agreement that went `active` with no meter able to measure export would
 * promise credit the system cannot calculate (rule 6).
 *
 * So the row offers "Order the inspection", then "Register and approve" once
 * the three preconditions hold. Rejection is still one click behind a
 * confirmation, because it is neither expected nor reversible.
 */
export default function GovernmentAgreements() {
  // Marks this list seen on open and hands back the watermark it
  // replaced, so rows that arrived since the last visit are lit for
  // exactly this render and normal on the next load.
  const watermark = useMarkViewSeen(VIEWS.governmentAgreements);
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState<string | null>(null);

  const pending = useQuery({
    queryKey: queryKeys.pendingAgreements(),
    queryFn: api.pendingAgreements,
  });

  // The inspections, so the queue can show who is going and how it went. One
  // request for all of them rather than one per row.
  const orders = useQuery({
    queryKey: queryKeys.workOrders(),
    queryFn: api.listWorkOrders,
  });
  const orderFor = (agreementId: string): WorkOrder | undefined =>
    (orders.data ?? [])
      .filter((o) => o.agreement_id === agreementId)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];

  const refresh = async () => {
    setConfirming(null);
    // The decided row leaves the pending list, and a newly active agreement
    // changes what the by-area rollup will report next time it is read.
    await queryClient.invalidateQueries({
      queryKey: queryKeys.pendingAgreements(),
    });
    await queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() });
    await queryClient.invalidateQueries({
      queryKey: queryKeys.analyticsByArea(),
    });
  };

  const decide = useMutation({
    mutationFn: ({
      agreementId,
      status,
    }: {
      agreementId: string;
      status: AgreementDecision;
    }) => api.decideAgreement(agreementId, status),
    onSuccess: refresh,
  });

  const order = useMutation({
    mutationFn: (agreementId: string) => api.raiseAgreementWorkOrder(agreementId),
    onSuccess: refresh,
  });

  const register = useMutation({
    mutationFn: (agreementId: string) => api.registerAgreementMeter(agreementId),
    onSuccess: refresh,
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
              unread={isUnread(agreement.created_at, watermark)}
              key={agreement.agreement_id}
              agreement={agreement}
              visit={orderFor(agreement.agreement_id)}
              confirming={confirming === agreement.agreement_id}
              onConfirmReject={() => setConfirming(agreement.agreement_id)}
              onCancelReject={() => setConfirming(null)}
              onDecide={(status) =>
                decide.mutate({ agreementId: agreement.agreement_id, status })
              }
              onOrder={() => order.mutate(agreement.agreement_id)}
              onRegister={() => register.mutate(agreement.agreement_id)}
              pending={
                (decide.isPending &&
                  decide.variables?.agreementId === agreement.agreement_id) ||
                (order.isPending &&
                  order.variables === agreement.agreement_id) ||
                (register.isPending &&
                  register.variables === agreement.agreement_id)
              }
            />
          ))}
        </ul>
      )}

      {(decide.error ?? order.error ?? register.error) && (
        <p className="border-t border-hairline px-5 py-3 text-xs text-status-critical">
          {(() => {
            const e = decide.error ?? order.error ?? register.error;
            // A 409 from /register is not a race -- it is the server naming
            // which precondition is missing. Show what it said.
            return e instanceof ApiError && typeof e.detail === "string"
              ? e.detail
              : `Could not record that: ${e!.message}`;
          })()}
        </p>
      )}
    </Card>
  );
}

function AgreementRow({
  unread,
  agreement,
  visit,
  confirming,
  onConfirmReject,
  onCancelReject,
  onDecide,
  onOrder,
  onRegister,
  pending,
}: {
  /** Arrived since this account last opened the list. */
  unread: boolean;
  agreement: Agreement;
  visit: WorkOrder | undefined;
  confirming: boolean;
  onConfirmReject: () => void;
  onCancelReject: () => void;
  onDecide: (status: AgreementDecision) => void;
  onOrder: () => void;
  onRegister: () => void;
  pending: boolean;
}) {
  // The same derivation the household reads, so the two sides cannot describe
  // one application differently -- see lib/fulfilment.ts.
  const stage = netMeteringStage(agreement.status, visitOf(visit));

  // Guarded server-side on all three; this only decides whether to offer the
  // button, so an official is not clicking to find out.
  const readyToRegister =
    visit?.status === "completed" &&
    !!visit.installed_serial_no &&
    !!visit.consumer_confirmed_at;
  const needsVisit = !visit || ["cancelled", "failed"].includes(visit.status);

  return (
    <li className={`px-5 py-4 ${unreadRowClass(unread)}`}>
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
            {needsVisit && (
              <button
                type="button"
                disabled={pending}
                onClick={onOrder}
                className="rounded-md bg-portal-government px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {visit ? "Order another inspection" : "Order the inspection"}
              </button>
            )}
            {readyToRegister && (
              <button
                type="button"
                disabled={pending}
                onClick={onRegister}
                className="rounded-md bg-portal-government px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {pending
                  ? "Registering…"
                  : `Register ${visit?.installed_serial_no} and approve`}
              </button>
            )}
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

      <div className="mt-2 flex flex-wrap items-start gap-2">
        <Badge tone={stage.tone}>{stage.label}</Badge>
        <p className="min-w-0 flex-1 text-sm text-ink-2">{stage.detail}</p>
      </div>

      {visit && <DispatchVisit order={visit} district={agreement.district} />}

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

/**
 * A `WorkOrder` seen as an `ApplicationVisit`.
 *
 * Same narrowing as the meter queue's: the two carry the same facts from
 * opposite ends, so the stage logic is shared rather than duplicated.
 */
function visitOf(order: WorkOrder | undefined) {
  if (!order) return null;
  return {
    order_id: order.order_id,
    order_type: order.order_type,
    status: order.status,
    scheduled_for: order.scheduled_for,
    started_at: order.started_at,
    completed_at: order.completed_at,
    completion_notes: order.completion_notes,
    failure_reason: order.failure_reason,
    installed_serial_no: order.installed_serial_no,
    consumer_confirmed_at: order.consumer_confirmed_at,
    consumer_disputed_at: order.consumer_disputed_at,
    consumer_note: order.consumer_note,
  };
}
