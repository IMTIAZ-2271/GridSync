import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type Assignment,
  type WorkOrder,
  type WorkOrderStatus,
} from "../lib/api";
import {
  BUCKETS,
  ORDER_STATUS_TONE,
  ORDER_TYPE_LABEL,
  bucketOf,
  transitionsFrom,
  type OrderBucket,
} from "../lib/workOrders";
import { humanize } from "../lib/issues";
import { useAuth } from "../auth/AuthContext";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * The field engineer's queue.
 *
 * `GET /api/work-orders` is already scoped for this role -- `work_orders_for_worker`
 * returns only orders this account is assigned to, so there is no filtering to
 * do here and no "all orders" view to accidentally expose. The assignment list
 * on each order still names the whole crew, which is deliberate: who else is
 * on a two-person meter swap is part of the job.
 */
export default function WorkerOrders() {
  const { account } = useAuth();
  const queryClient = useQueryClient();
  const [bucket, setBucket] = useState<OrderBucket>("active");

  const orders = useQuery({
    queryKey: queryKeys.workOrders(),
    queryFn: api.listWorkOrders,
  });

  const counts = useMemo(() => {
    const empty: Record<OrderBucket, number> = {
      active: 0,
      upcoming: 0,
      closed: 0,
    };
    for (const o of orders.data ?? []) empty[bucketOf(o.status)] += 1;
    return empty;
  }, [orders.data]);

  const shown = (orders.data ?? []).filter((o) => bucketOf(o.status) === bucket);

  const advance = useMutation({
    mutationFn: ({ orderId, status }: { orderId: string; status: WorkOrderStatus }) =>
      api.updateWorkOrderStatus(orderId, status),
    // Refetch rather than patching the cache by hand: the server maintains
    // started_at and completed_at inside update_work_order_status, so the row
    // that comes back carries timestamps the client never computed and must
    // not guess at.
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() }),
  });

  return (
    <Card>
      <CardHeader
        title="Work orders"
        subtitle={
          account ? `Jobs assigned to ${account.full_name}` : "Assigned jobs"
        }
        action={
          <nav className="flex gap-1" aria-label="Queue">
            {BUCKETS.map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => setBucket(b.id)}
                aria-current={bucket === b.id}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  bucket === b.id
                    ? "bg-portal-worker text-white"
                    : "text-ink-2 hover:bg-hairline/60"
                }`}
              >
                {b.label}
                <span className="tabular ml-1.5 opacity-70">
                  {counts[b.id]}
                </span>
              </button>
            ))}
          </nav>
        }
      />

      {orders.isPending ? (
        <div className="space-y-3 p-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-24 w-full" />
          ))}
        </div>
      ) : orders.error ? (
        <ErrorState error={orders.error} />
      ) : shown.length === 0 ? (
        <EmptyState
          title={`Nothing ${bucket}`}
          hint={
            bucket === "active"
              ? "No job is dispatched or under way right now."
              : bucket === "upcoming"
                ? "Nothing is drafted or scheduled for you."
                : "No job has been completed, failed or cancelled yet."
          }
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {shown.map((order) => (
            <OrderRow
              key={order.order_id}
              order={order}
              myAccountId={account?.account_id}
              onAdvance={(status) =>
                advance.mutate({ orderId: order.order_id, status })
              }
              pending={
                advance.isPending && advance.variables?.orderId === order.order_id
              }
            />
          ))}
        </ul>
      )}

      {advance.error && (
        <p className="border-t border-hairline px-5 py-3 text-xs text-status-critical">
          {advance.error instanceof ApiError && advance.error.status === 404
            ? "That order is no longer assigned to you."
            : `Could not update that order: ${advance.error.message}`}
        </p>
      )}
    </Card>
  );
}

function OrderRow({
  order,
  myAccountId,
  onAdvance,
  pending,
}: {
  order: WorkOrder;
  myAccountId?: string;
  onAdvance: (status: WorkOrderStatus) => void;
  pending: boolean;
}) {
  const transitions = transitionsFrom(order.status);
  const mine = order.assignments.find((a) => a.account_id === myAccountId);

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="flex flex-wrap items-center gap-2 text-sm font-medium text-ink">
            {ORDER_TYPE_LABEL[order.order_type] ?? humanize(order.order_type)}
            <Badge tone={ORDER_STATUS_TONE[order.status]}>
              {humanize(order.status)}
            </Badge>
            {/* Priority 1 is the top of the scale in this schema, not the
                bottom. Only call it out when it actually is urgent. */}
            {order.priority <= 2 && <Badge tone="serious">priority {order.priority}</Badge>}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {order.site_label}
            {mine && ` · you are ${mine.job_role}`}
            {order.device_id && " · device-specific"}
            {order.issue_id && " · raised from an issue"}
          </p>
        </div>

        {transitions.length > 0 && (
          <div className="flex shrink-0 flex-wrap gap-2">
            {transitions.map((t) => (
              <button
                key={t.to}
                type="button"
                disabled={pending}
                onClick={() => onAdvance(t.to)}
                className={
                  t.emphasis === "primary"
                    ? "rounded-md bg-portal-worker px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
                    : "rounded-md border border-hairline px-2.5 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-plane disabled:opacity-40"
                }
              >
                {t.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
        <Figure label="Scheduled" value={formatWhen(order.scheduled_for)} />
        <Figure label="Started" value={formatWhen(order.started_at)} />
        <Figure label="Completed" value={formatWhen(order.completed_at)} />
        <Figure label="Crew" value={<Crew assignments={order.assignments} />} />
      </dl>

      {order.completion_notes && (
        <p className="mt-2 text-sm text-ink-2">{order.completion_notes}</p>
      )}
      {order.failure_reason && (
        <p className="mt-2 text-sm text-status-critical">
          {order.failure_reason}
        </p>
      )}
    </li>
  );
}

/** Every assignee, not just the reader -- see the note on this page. */
function Crew({ assignments }: { assignments: Assignment[] }) {
  if (assignments.length === 0) return <>unassigned</>;
  return (
    <>
      {assignments
        .map((a) => `${a.worker_name} (${a.job_role})`)
        .join(", ")}
    </>
  );
}

function formatWhen(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function Figure({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="mt-0.5 font-medium text-ink-2">{value}</dd>
    </div>
  );
}
