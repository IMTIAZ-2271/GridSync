import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type Assignment,
  type WorkOrder,
} from "../lib/api";
import { ORDER_STATUS_TONE } from "../lib/workOrders";
import { Badge } from "./ui";

/**
 * The officer's half of a visit: who is going, and when.
 *
 * Deliberately a control on the application row rather than a screen of its
 * own. An official dispatching a meter install is doing it *about* one
 * household's application — they are not balancing a fleet queue, which is
 * what /supplier/dispatch exists for — so the picker belongs where the context
 * is.
 *
 * Everything here goes through the endpoints a supplier's dispatcher already
 * uses: `POST /api/work-orders/{id}/assignments` starts the same three-hour
 * offer clock, `offer_assignment` applies the same district rule (worker
 * requirement 4), and the same jobs sweeps expire it. Nothing about the
 * assignment lifecycle is special-cased for government orders.
 */
export default function DispatchVisit({
  order,
  district,
}: {
  order: WorkOrder;
  district: string;
}) {
  const queryClient = useQueryClient();
  const [picking, setPicking] = useState(false);

  // Fetched per district, matching the rule the offer itself enforces: a
  // technician only receives requests from their own region, so a picker
  // offering anyone else would be offering a name the next call refuses.
  const workers = useQuery({
    queryKey: queryKeys.assignableWorkers(district),
    queryFn: () => api.assignableWorkers(district),
    enabled: picking,
  });

  const offer = useMutation({
    mutationFn: (accountId: string) =>
      api.offerAssignment(order.order_id, accountId),
    onSuccess: async () => {
      setPicking(false);
      await queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() });
    },
  });

  // Only live assignments describe who is going. A declined or expired one is
  // history, and showing it as "assigned" would tell an official the visit is
  // covered when it is not.
  const live = order.assignments.filter(
    (a) => a.status === "offered" || a.status === "accepted",
  );

  return (
    <div className="mt-3 rounded-lg bg-plane p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-ink-muted">
          Visit
        </span>
        <Badge tone={ORDER_STATUS_TONE[order.status]}>
          {order.status.replace(/_/g, " ")}
        </Badge>
        {order.installed_serial_no && (
          <span className="text-xs text-ink-2">
            fitted{" "}
            <span className="font-mono text-ink">
              {order.installed_serial_no}
            </span>
          </span>
        )}
      </div>

      {live.length > 0 ? (
        <ul className="mt-2 space-y-1">
          {live.map((a) => (
            <li key={a.account_id} className="text-sm text-ink-2">
              {a.worker_name} — {a.status}
              <Clock assignment={a} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-sm text-ink-2">
          Nobody is assigned to this visit yet.
        </p>
      )}

      {order.status !== "completed" && order.status !== "cancelled" && (
        <div className="mt-2">
          {!picking ? (
            <button
              type="button"
              onClick={() => setPicking(true)}
              className="text-sm text-ink-muted underline"
            >
              {live.length > 0 ? "Offer to someone else" : "Assign a technician"}
            </button>
          ) : workers.isPending ? (
            <p className="text-sm text-ink-muted">Loading technicians…</p>
          ) : workers.error ? (
            <p className="text-sm text-status-critical">
              Could not load technicians.
            </p>
          ) : workers.data.length === 0 ? (
            <p className="text-sm text-ink-2">
              No approved technician serves {district}. Approve one on the
              Worker approvals page first.
            </p>
          ) : (
            <ul className="mt-1 space-y-1">
              {workers.data.map((w) => (
                <li
                  key={w.account_id}
                  className="flex flex-wrap items-center gap-2 text-sm"
                >
                  <span className="text-ink">{w.full_name}</span>
                  <span className="text-xs text-ink-muted">
                    {w.open_jobs} open
                    {w.rating_avg ? ` · ${w.rating_avg}★` : " · unrated"}
                  </span>
                  <button
                    type="button"
                    disabled={offer.isPending}
                    onClick={() => offer.mutate(w.account_id)}
                    className="ml-auto rounded-md bg-ink px-2.5 py-1 text-xs font-medium text-surface disabled:opacity-50"
                  >
                    {offer.isPending ? "Offering…" : "Offer"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {offer.error && (
        <p className="mt-2 text-sm text-status-critical">
          {offer.error instanceof ApiError &&
          typeof offer.error.detail === "string"
            ? offer.error.detail
            : offer.error.message}
        </p>
      )}
    </div>
  );
}

/**
 * How long is left, in words.
 *
 * The sweeps run every five minutes, so a ticking second-by-second timer would
 * imply a precision the system does not have.
 */
function Clock({ assignment }: { assignment: Assignment }) {
  const at =
    assignment.status === "offered"
      ? assignment.offer_expires_at
      : assignment.start_deadline_at;
  if (!at) return null;
  const ms = new Date(at).getTime() - Date.now();
  if (ms <= 0) return <span className="text-ink-muted"> · overdue</span>;
  const hours = Math.round(ms / 3_600_000);
  const label =
    hours < 1
      ? "under an hour"
      : hours < 48
        ? `about ${hours} hour${hours === 1 ? "" : "s"}`
        : `about ${Math.round(hours / 24)} days`;
  return (
    <span className="text-ink-muted">
      {" "}
      · {assignment.status === "offered" ? "expires in" : "due to start in"}{" "}
      {label}
    </span>
  );
}
