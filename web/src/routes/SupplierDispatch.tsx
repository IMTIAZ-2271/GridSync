import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type DispatchableIssue,
  type WorkOrder,
  type WorkOrderType,
} from "../lib/api";
import { ORDER_TYPE_LABEL } from "../lib/workOrders";
import { SEVERITY_TONE, categoryLabel, humanize } from "../lib/issues";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Supplier requirement 3: turn a complaint into a visit, and put a name on it.
 *
 * Two queues, in the order the work actually flows. **Unassigned complaints**
 * are issues with no live work order against them; raising one produces a
 * `draft` order. **Waiting for a technician** are orders with nobody on them,
 * which is both freshly raised orders and orders the deadline sweep handed
 * back -- the two are indistinguishable from here on purpose, because they need
 * exactly the same thing next.
 *
 * Offering starts a three-hour clock the jobs runner sweeps. That is why the
 * worker list shows open jobs rather than just names: an offer to someone
 * already holding four is capacity that will most likely lapse, and the point
 * of the number is to make that visible before the click rather than three
 * hours after it.
 *
 * Ratings are the sort key supplier requirement 4 asks for, and households now
 * write them from /consumer/visits. A technician nobody has rated still reads
 * "not yet rated" and sorts below the rated rather than above -- absent is not
 * the same fact as zero.
 *
 * The technician list is fetched per order, for that order's district: worker
 * requirement 4 says a technician only receives requests from their own region,
 * and the API enforces it with a 409, so a whole-fleet list would offer names
 * the next click would reject.
 */

/** Which visit a complaint most likely needs. A default, never a decision. */
const SUGGESTED_TYPE: Record<string, WorkOrderType> = {
  meter_fault: "meter_swap",
  inverter_fault: "inverter_service",
  data_gap: "inspection",
  export_not_credited: "seal_check",
  outage: "inspection",
  billing_dispute: "inspection",
  solar_installation: "inspection",
  supplier_service: "inspection",
  net_metering: "seal_check",
  other: "inspection",
};

const ORDER_TYPES = Object.keys(ORDER_TYPE_LABEL) as WorkOrderType[];

export default function SupplierDispatch() {
  const queryClient = useQueryClient();

  const issues = useQuery({
    queryKey: queryKeys.dispatchableIssues(),
    queryFn: api.dispatchableIssues,
  });
  const orders = useQuery({
    queryKey: queryKeys.workOrders(),
    queryFn: api.listWorkOrders,
  });

  const refreshAll = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.dispatchableIssues() }),
      queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() }),
      // The offer changes someone's open-job count, which is the number the
      // next decision is made on.
      queryClient.invalidateQueries({ queryKey: ["workers"] }),
    ]);

  const raise = useMutation({
    mutationFn: ({ issueId, type }: { issueId: string; type: WorkOrderType }) =>
      api.createWorkOrder({ issue_id: issueId, order_type: type }),
    onSuccess: refreshAll,
  });

  const offer = useMutation({
    mutationFn: ({ orderId, accountId }: { orderId: string; accountId: string }) =>
      api.offerAssignment(orderId, accountId, "lead"),
    onSuccess: refreshAll,
  });

  // An order nobody is on. Freshly raised and swept-back look the same here,
  // because they need the same thing next.
  const unassigned = useMemo(
    () =>
      (orders.data ?? []).filter(
        (o) =>
          !["completed", "cancelled", "failed"].includes(o.status) &&
          !o.assignments.some((a) => ["offered", "accepted"].includes(a.status)),
      ),
    [orders.data],
  );

  const awaitingAnswer = useMemo(
    () =>
      (orders.data ?? []).filter((o) =>
        o.assignments.some((a) => a.status === "offered"),
      ),
    [orders.data],
  );

  const error = raise.error ?? offer.error;

  return (
    <div className="flex flex-col gap-6">
      {error && (
        <Card>
          <p className="px-5 py-3 text-sm text-status-critical">
            {error instanceof ApiError ? String(error.detail) : error.message}
          </p>
        </Card>
      )}

      <Card>
        <CardHeader
          title="Unassigned complaints"
          subtitle="Reported faults with no visit raised against them"
          action={
            issues.data ? (
              <Badge tone={issues.data.length > 0 ? "warning" : "good"}>
                {issues.data.length} waiting
              </Badge>
            ) : undefined
          }
        />
        {issues.isPending ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : issues.error ? (
          <ErrorState error={issues.error} />
        ) : issues.data.length === 0 ? (
          <EmptyState
            title="Nothing waiting"
            hint="Every open complaint already has a visit raised against it. An issue whose visit was cancelled or failed comes back here."
          />
        ) : (
          <ul className="divide-y divide-hairline">
            {issues.data.map((issue) => (
              <IssueRow
                key={issue.issue_id}
                issue={issue}
                busy={raise.isPending}
                onRaise={(type) =>
                  raise.mutate({ issueId: issue.issue_id, type })
                }
              />
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Waiting for a technician"
          subtitle="Raised, or handed back when an offer or a start deadline lapsed"
          action={
            orders.data ? (
              <Badge tone={unassigned.length > 0 ? "warning" : "good"}>
                {unassigned.length} to assign
              </Badge>
            ) : undefined
          }
        />
        {orders.isPending ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-16 w-full" />
          </div>
        ) : orders.error ? (
          <ErrorState error={orders.error} />
        ) : unassigned.length === 0 ? (
          <EmptyState
            title="Everything is with someone"
            hint="Orders appear here the moment they are raised, and again if nobody answers the offer within three hours."
          />
        ) : (
          <ul className="divide-y divide-hairline">
            {unassigned.map((order) => (
              <AssignRow
                key={order.order_id}
                order={order}
                busy={offer.isPending}
                onOffer={(accountId) =>
                  offer.mutate({ orderId: order.order_id, accountId })
                }
              />
            ))}
          </ul>
        )}
      </Card>

      {awaitingAnswer.length > 0 && (
        <Card>
          <CardHeader
            title="Offered, awaiting an answer"
            subtitle="Unanswered after three hours, these return to the queue above"
          />
          <ul className="divide-y divide-hairline">
            {awaitingAnswer.map((order) => {
              const offered = order.assignments.find(
                (a) => a.status === "offered",
              )!;
              return (
                <li
                  key={order.order_id}
                  className="flex flex-wrap items-baseline justify-between gap-3 px-5 py-3"
                >
                  <span className="text-sm text-ink">
                    <b className="font-medium">
                      {ORDER_TYPE_LABEL[order.order_type]}
                    </b>{" "}
                    at {order.site_label} &middot; {offered.worker_name}
                  </span>
                  <Countdown until={offered.offer_expires_at} />
                </li>
              );
            })}
          </ul>
        </Card>
      )}
    </div>
  );
}

/**
 * How long is left, in words.
 *
 * Not a ticking timer: the deadline is stored, the sweep runs every five
 * minutes, and a second-by-second countdown would imply a precision the system
 * does not have. "in about 2 hours" is true; "01:59:47" is theatre.
 */
function Countdown({ until }: { until: string | null }) {
  if (!until) return null;
  const mins = Math.round((+new Date(until) - Date.now()) / 60000);
  if (mins <= 0) {
    return <Badge tone="critical">due &mdash; returning to the queue</Badge>;
  }
  const text =
    mins < 60
      ? `${mins} min left`
      : `about ${Math.round(mins / 60)} hour${mins >= 90 ? "s" : ""} left`;
  return <Badge tone={mins < 30 ? "warning" : "neutral"}>{text}</Badge>;
}

function IssueRow({
  issue,
  busy,
  onRaise,
}: {
  issue: DispatchableIssue;
  busy: boolean;
  onRaise: (type: WorkOrderType) => void;
}) {
  const [type, setType] = useState<WorkOrderType>(
    SUGGESTED_TYPE[issue.category] ?? "inspection",
  );
  const age = Math.floor((Date.now() - +new Date(issue.reported_at)) / 86_400_000);

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="font-medium text-ink">{issue.title}</p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {issue.site_label} &middot; {issue.district} &middot;{" "}
            {categoryLabel(issue.category)} &middot; {issue.reported_by_name}{" "}
            &middot;{" "}
            {age === 0 ? "today" : `${age} day${age === 1 ? "" : "s"} ago`}
            {issue.device_serial && ` · ${issue.device_serial}`}
          </p>
        </div>
        <Badge tone={SEVERITY_TONE[issue.severity]}>{issue.severity}</Badge>
      </div>

      {issue.description && (
        <p className="mt-2 text-sm text-ink-2">{issue.description}</p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="text-xs text-ink-muted" htmlFor={`t-${issue.issue_id}`}>
          Visit type
        </label>
        <select
          id={`t-${issue.issue_id}`}
          value={type}
          onChange={(e) => setType(e.target.value as WorkOrderType)}
          className="rounded-lg border border-hairline bg-surface px-2.5 py-1.5 text-sm text-ink"
        >
          {ORDER_TYPES.map((t) => (
            <option key={t} value={t}>
              {ORDER_TYPE_LABEL[t]}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy}
          onClick={() => onRaise(type)}
          className="rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
        >
          Raise work order
        </button>
      </div>
    </li>
  );
}

function AssignRow({
  order,
  busy,
  onOffer,
}: {
  order: WorkOrder;
  busy: boolean;
  onOffer: (accountId: string) => void;
}) {
  const [who, setWho] = useState("");

  // Worker requirement 4: technicians only receive requests from their own
  // region. The list is fetched FOR the order's district rather than fetched
  // whole and filtered here -- the API refuses a cross-district offer with a
  // 409, so showing names it would reject would be offering a button that
  // cannot work. The server still sorts best-rated then least-loaded.
  const workers = useQuery({
    queryKey: queryKeys.assignableWorkers(order.district),
    queryFn: () => api.assignableWorkers(order.district),
  });

  const lapsed = order.assignments.filter((a) =>
    ["expired", "declined"].includes(a.status),
  );

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="font-medium text-ink">
            {ORDER_TYPE_LABEL[order.order_type]} &middot; {order.site_label}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {order.district} · {humanize(order.status)}
            {order.priority <= 2 && ` · priority ${order.priority}`}
            {lapsed.length > 0 &&
              ` · ${lapsed.length} previous offer${
                lapsed.length === 1 ? "" : "s"
              } lapsed or declined`}
          </p>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          aria-label="Technician"
          value={who}
          onChange={(e) => setWho(e.target.value)}
          className="min-w-[18rem] rounded-lg border border-hairline bg-surface px-2.5 py-1.5 text-sm text-ink"
        >
          <option value="">
            {workers.isPending
              ? "Loading…"
              : (workers.data ?? []).length === 0
                ? `No technician serves ${order.district}`
                : "Choose a technician…"}
          </option>
          {(workers.data ?? []).map((w) => (
            <option key={w.account_id} value={w.account_id}>
              {w.full_name} — {w.service_district} —{" "}
              {w.rating_avg ? `${w.rating_avg}★` : "not yet rated"} —{" "}
              {w.open_jobs}/{w.max_daily_jobs} jobs
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy || !who || workers.isPending}
          onClick={() => onOffer(who)}
          className="rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
        >
          Offer &mdash; 3 hours to answer
        </button>
      </div>
    </li>
  );
}
