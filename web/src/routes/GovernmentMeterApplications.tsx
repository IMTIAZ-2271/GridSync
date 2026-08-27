import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type MeterApplicationQueueRow,
  type WorkOrder,
} from "../lib/api";
import DispatchVisit from "../components/DispatchVisit";
import { meterApplicationStage } from "../lib/fulfilment";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Meter applications: households asking for hardware, in this official's
 * district.
 *
 * The other end of consumer requirement 6. Since migration b7d3f5a92c14 this
 * queue does not hand over a meter -- it orders a visit:
 *
 *   order the installation -> offer it to a technician -> they fit the meter
 *   and record its serial -> the household confirms -> register it
 *
 * **Register is the only thing that issues a `meter_asset`**, and the server
 * refuses it until the visit is complete, the serial is recorded and the
 * household has confirmed. So the button below is not the decision -- the
 * three facts are, and the button only becomes useful once they are true.
 *
 * Scope is the official's own district and comes from the server, not from a
 * filter here; an application from a neighbouring district answers 404, not
 * 403, because confirming it exists would tell a stranger who has applied for a
 * meter next door. There is no district selector for the same reason there is
 * none on the worker queue: an official governs one district.
 *
 * Oldest-first. A queue sorted by recency buries whoever nobody picked up.
 *
 * Refusal asks for a reason; ordering a visit asks for nothing -- the same
 * asymmetry as the other two queues. A rejection the applicant cannot act on
 * is worse than no answer.
 *
 * There is no serial field here on purpose. The technician holding the meter
 * records it, and this page reads it back; an official typing a number for
 * hardware they never saw is the gap the whole flow closes.
 */
export default function GovernmentMeterApplications() {
  const queryClient = useQueryClient();
  const [includeDecided, setIncludeDecided] = useState(false);
  const [acting, setActing] = useState<string | null>(null);
  const [notes, setNotes] = useState("");

  const queue = useQuery({
    queryKey: queryKeys.meterApplicationQueue(includeDecided),
    queryFn: () => api.meterApplicationQueue(includeDecided),
  });

  // The visits, so the queue can show who is going and how it went. One
  // request for all of them rather than one per row: the official's queue is a
  // page of applications, and a fetch per card would be N+1 on the screen they
  // open most.
  const orders = useQuery({
    queryKey: queryKeys.workOrders(),
    queryFn: api.listWorkOrders,
  });
  const orderFor = (applicationId: string): WorkOrder | undefined =>
    (orders.data ?? [])
      .filter((o) => o.meter_application_id === applicationId)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];

  const refresh = async () => {
    await queryClient.invalidateQueries({
      queryKey: queryKeys.meterApplicationQueue(includeDecided),
    });
    await queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() });
  };

  const order = useMutation({
    mutationFn: (id: string) => api.raiseMeterWorkOrder(id),
    onSettled: refresh,
  });

  const register = useMutation({
    mutationFn: (id: string) => api.registerAppliedMeter(id),
    onSettled: async () => {
      await refresh();
      setActing(null);
      setNotes("");
    },
  });

  const decide = useMutation({
    mutationFn: ({ id }: { id: string }) =>
      api.decideMeterApplication(id, {
        status: "rejected",
        decision_notes: notes.trim() || null,
      }),
    // Invalidate rather than splice: the server stamps decided_at, and a
    // decision someone else made in the meantime should surface rather than be
    // papered over.
    onSettled: async () => {
      await refresh();
      setActing(null);
      setNotes("");
    },
  });

  const failure = decide.error ?? order.error ?? register.error;
  const conflict = failure instanceof ApiError && failure.status === 409;
  const open = (queue.data ?? []).filter(
    (a) => a.status === "submitted" || a.status === "under_review",
  );

  return (
    <Card>
      <CardHeader
        title="Meter applications"
        subtitle="Households in your district asking to be issued a meter"
        action={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-ink-2">
              <input
                type="checkbox"
                checked={includeDecided}
                onChange={(e) => setIncludeDecided(e.target.checked)}
              />
              Show decided
            </label>
            {queue.data && (
              <Badge tone={open.length > 0 ? "warning" : "good"}>
                {open.length} waiting
              </Badge>
            )}
          </div>
        }
      />

      {/* A 409 here is usually not a race -- it is the server explaining which
          of registration's three preconditions is not met yet. Show what it
          said rather than a generic "someone beat you to it". */}
      {failure && (
        <p
          className={`border-b border-hairline px-5 py-3 text-sm ${
            conflict ? "text-ink-2" : "text-status-critical"
          }`}
        >
          {failure instanceof ApiError && typeof failure.detail === "string"
            ? failure.detail
            : failure.message}
        </p>
      )}

      {queue.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : queue.error ? (
        <ErrorState error={queue.error} />
      ) : queue.data.length === 0 ? (
        <EmptyState
          title="Nothing waiting"
          hint="A household with no spare meter applies through one of its sites, and the request appears here. You order the installation; the meter is registered once the work is done and the household has confirmed it."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {queue.data.map((app) => (
            <ApplicationRow
              key={app.application_id}
              app={app}
              visit={orderFor(app.application_id)}
              rejecting={acting === app.application_id}
              notes={notes}
              busy={decide.isPending || order.isPending || register.isPending}
              onNotes={setNotes}
              onStartReject={() => {
                setActing(app.application_id);
                setNotes("");
              }}
              onCancel={() => setActing(null)}
              onOrder={() => order.mutate(app.application_id)}
              onRegister={() => register.mutate(app.application_id)}
              onReject={() => decide.mutate({ id: app.application_id })}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

const STATUS: Record<string, { label: string; tone: string }> = {
  submitted: { label: "Submitted", tone: "warning" },
  under_review: { label: "Visit ordered", tone: "info" },
  accepted: { label: "Registered", tone: "good" },
  rejected: { label: "Rejected", tone: "critical" },
  withdrawn: { label: "Withdrawn", tone: "neutral" },
};

function ApplicationRow({
  app,
  visit,
  rejecting,
  notes,
  busy,
  onNotes,
  onStartReject,
  onCancel,
  onOrder,
  onRegister,
  onReject,
}: {
  app: MeterApplicationQueueRow;
  visit: WorkOrder | undefined;
  rejecting: boolean;
  notes: string;
  busy: boolean;
  onNotes: (v: string) => void;
  onStartReject: () => void;
  onCancel: () => void;
  onOrder: () => void;
  onRegister: () => void;
  onReject: () => void;
}) {
  const open = app.status === "submitted" || app.status === "under_review";
  const meta = STATUS[app.status] ?? { label: app.status, tone: "neutral" };

  // The same derivation the household reads on its own page, so the two sides
  // cannot describe one application differently. `issued_meter_available` is
  // not on the queue projection, and does not matter here: once it is
  // registered the official's work is done either way.
  const stage = meterApplicationStage(app.status, visitOf(visit), null);

  // Registration is guarded server-side on all three; this only decides
  // whether to *offer* the button, so an official is not clicking to find out.
  const readyToRegister =
    open &&
    visit?.status === "completed" &&
    !!visit.installed_serial_no &&
    !!visit.consumer_confirmed_at;

  const needsVisit =
    open && (!visit || ["cancelled", "failed"].includes(visit.status));

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">
            {app.account_name}
            <span className="ml-2 font-normal text-ink-muted">
              {app.site_label}
            </span>
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {app.address_line} · {app.district}
            {app.national_id && (
              <>
                {" · NID "}
                <span className="font-mono">{app.national_id}</span>
              </>
            )}
            {app.phone && <> · {app.phone}</>}
          </p>
          <p className="mt-1 text-xs text-ink-2">
            {/* The one fact that turns "they want a meter" into a decision. A
                first connection and a fourth are not the same request. */}
            This address has {app.existing_meters} meter
            {app.existing_meters === 1 ? "" : "s"} · applied{" "}
            {new Date(app.submitted_at).toLocaleDateString(undefined, {
              dateStyle: "medium",
            })}
          </p>
          {app.reason && <p className="mt-1 text-sm text-ink-2">{app.reason}</p>}

          <div className="mt-2 flex flex-wrap items-start gap-2">
            <Badge tone={stage.tone}>{stage.label}</Badge>
            <p className="min-w-0 flex-1 text-sm text-ink-2">{stage.detail}</p>
          </div>

          {app.decision_notes && (
            <p className="mt-1 text-sm text-ink-2">
              Decision: {app.decision_notes}
            </p>
          )}
          {app.issued_serial_no && (
            <p className="mt-1 text-sm text-ink-2">
              Registered{" "}
              <span className="font-mono">{app.issued_serial_no}</span> to the
              household
            </p>
          )}

          {visit && <DispatchVisit order={visit} district={app.district} />}
        </div>
        <Badge tone={meta.tone}>{meta.label}</Badge>
      </div>

      {open && !rejecting && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {needsVisit && (
            <button
              type="button"
              disabled={busy}
              onClick={onOrder}
              className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {visit ? "Order another visit" : "Order the installation"}
            </button>
          )}
          {readyToRegister && (
            <button
              type="button"
              disabled={busy}
              onClick={onRegister}
              className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy
                ? "Registering…"
                : `Register ${visit?.installed_serial_no} to them`}
            </button>
          )}
          <button
            type="button"
            onClick={onStartReject}
            className="text-sm text-ink-muted underline"
          >
            Reject
          </button>
        </div>
      )}

      {open && rejecting && (
        <div className="mt-3 space-y-3 rounded-lg bg-plane p-3">
          <label className="block text-sm">
            <span className="font-medium text-ink-2">Reason</span>
            <input
              type="text"
              value={notes}
              onChange={(e) => onNotes(e.target.value)}
              placeholder="This address already has three connections"
              className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
            />
            <span className="mt-1 block text-xs text-ink-muted">
              The applicant sees this.
            </span>
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={busy || !notes.trim()}
              onClick={onReject}
              className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy ? "Saving…" : "Reject application"}
            </button>
            <button
              type="button"
              onClick={onCancel}
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

/**
 * A `WorkOrder` seen as an `ApplicationVisit`.
 *
 * The two carry the same facts from opposite ends -- the official reads the
 * order, the household reads the visit projected onto its application -- so
 * this narrows one to the other rather than duplicating the stage logic.
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
