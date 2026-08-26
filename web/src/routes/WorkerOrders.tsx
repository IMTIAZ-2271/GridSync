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

  // Answering an offer is a different act from advancing an order: it is the
  // worker's own two-party agreement, keyed on the token rather than on an id,
  // and accepting it starts the one-day start deadline the jobs runner sweeps.
  const respond = useMutation({
    mutationFn: ({
      orderId,
      decision,
      reason,
    }: {
      orderId: string;
      decision: "accept" | "decline";
      reason?: string;
    }) => api.respondToAssignment(orderId, decision, reason),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() }),
  });

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
              onRespond={(decision, reason) =>
                respond.mutate({ orderId: order.order_id, decision, reason })
              }
              pending={
                (advance.isPending &&
                  advance.variables?.orderId === order.order_id) ||
                (respond.isPending &&
                  respond.variables?.orderId === order.order_id)
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
  onRespond,
  pending,
}: {
  order: WorkOrder;
  myAccountId?: string;
  onAdvance: (status: WorkOrderStatus) => void;
  onRespond: (decision: "accept" | "decline", reason?: string) => void;
  pending: boolean;
}) {
  const transitions = transitionsFrom(order.status);
  const mine = order.assignments.find((a) => a.account_id === myAccountId);
  // An unanswered offer outranks everything else on the row: it has a clock on
  // it, and ignoring it hands the job to someone else in three hours.
  const unanswered = mine?.status === "offered";
  // Declining asks for a reason on the row, the same shape the government's
  // worker queue uses for a rejection. The dispatcher has to find somebody
  // else and "why" is most of what tells them who -- but it stays optional: a
  // technician made to justify a decline will either type nothing useful or
  // let the offer lapse instead, which costs the dispatcher three hours.
  const [declining, setDeclining] = useState(false);
  const [reason, setReason] = useState("");

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
          {mine && <Deadline assignment={mine} />}
        </div>

        {unanswered ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => setDeclining((v) => !v)}
              aria-expanded={declining}
              className="text-sm text-ink-muted underline disabled:opacity-50"
            >
              Decline
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => onRespond("accept")}
              className="rounded-lg bg-portal-worker px-3.5 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {pending ? "Saving…" : "Accept"}
            </button>
          </div>
        ) : transitions.length > 0 && (
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

      {unanswered && declining && (
        <div className="mt-3 rounded-md border border-hairline bg-plane/50 p-3">
          <label
            htmlFor={`decline-reason-${order.order_id}`}
            className="text-xs font-medium text-ink-2"
          >
            Why are you turning this down?{" "}
            <span className="font-normal text-ink-muted">
              Optional — the dispatcher sees it straight away.
            </span>
          </label>
          <input
            id={`decline-reason-${order.order_id}`}
            type="text"
            value={reason}
            maxLength={280}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Already booked in Gulshan that morning"
            className="mt-1.5 w-full rounded-md border border-hairline bg-surface px-2.5 py-1.5 text-sm text-ink placeholder:text-ink-muted"
          />
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => onRespond("decline", reason.trim() || undefined)}
              className="rounded-md bg-portal-worker px-2.5 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {pending ? "Saving…" : "Confirm decline"}
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => {
                setDeclining(false);
                setReason("");
              }}
              className="rounded-md border border-hairline px-2.5 py-1 text-xs font-medium text-ink-2 transition-colors hover:bg-plane disabled:opacity-40"
            >
              Keep the offer
            </button>
          </div>
        </div>
      )}

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

/**
 * The clock on this worker's own assignment, in words.
 *
 * Both deadlines are stored on the assignment and swept every five minutes by
 * services/jobs, so a second-by-second timer would imply a precision the system
 * does not have. What matters is whether there is time left and roughly how
 * much: an unanswered offer goes to somebody else, and an accepted job that is
 * never started goes back to the dispatcher.
 */
function Deadline({ assignment }: { assignment: Assignment }) {
  const offer = assignment.status === "offered" ? assignment.offer_expires_at : null;
  const start =
    assignment.status === "accepted" ? assignment.start_deadline_at : null;
  const at = offer ?? start;
  if (!at) return null;

  const mins = Math.round((+new Date(at) - Date.now()) / 60000);
  const overdue = mins <= 0;
  const left =
    mins >= 1440
      ? `${Math.round(mins / 1440)} day${mins >= 2160 ? "s" : ""}`
      : mins >= 60
        ? `${Math.round(mins / 60)} hour${mins >= 90 ? "s" : ""}`
        : `${Math.max(mins, 0)} min`;

  return (
    <p className="mt-1 text-xs">
      <span
        className={overdue ? "font-medium text-status-critical" : "text-ink-2"}
      >
        {offer
          ? overdue
            ? "This offer has lapsed — it will be released to someone else."
            : `Accept within ${left} or it goes to another technician.`
          : overdue
            ? "Overdue — start it or it goes back to the dispatcher."
            : `Start within ${left} or it goes back to the dispatcher.`}
      </span>
    </p>
  );
}
