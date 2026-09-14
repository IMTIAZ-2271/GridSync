import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, queryKeys, type WorkOrder } from "../lib/api";
import { ORDER_STATUS_TONE, ORDER_TYPE_LABEL } from "../lib/workOrders";
import { formatWhen } from "../lib/admin";
import { Badge, Card, CardHeader, EmptyState, ErrorState, Skeleton } from "../components/ui";

const FINISHED = new Set(["completed", "failed", "cancelled"]);

/**
 * Every open work order in the system, and the two ways an admin can step in.
 *
 * Release takes everyone off a stuck job and puts it back in the dispatcher's
 * queue; cancel ends it and tells the household. Both release every live
 * assignment, tell the people on it, and are recorded with the reason given.
 * Ordinary dispatching stays with the supplier's and the official's pages.
 */
export default function AdminWorkOrders() {
  const queryClient = useQueryClient();
  const [acting, setActing] = useState<{ id: string; action: "release" | "cancel" } | null>(null);
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<string | null>(null);

  const orders = useQuery({ queryKey: queryKeys.workOrders(), queryFn: api.listWorkOrders });

  const open = useMemo(
    () =>
      (orders.data ?? [])
        .filter((o) => !FINISHED.has(o.status))
        .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [orders.data],
  );

  const intervene = useMutation({
    mutationFn: () => api.adminInterveneWorkOrder(acting!.id, { action: acting!.action, reason: reason.trim() }),
    onSuccess: (result) => {
      setDone(
        result.status === "cancelled"
          ? "Order cancelled. Everyone involved has been told."
          : `Order returned to the dispatcher's queue; ${result.released.length} ${result.released.length === 1 ? "person was" : "people were"} taken off it.`,
      );
      setActing(null);
      setReason("");
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.workOrders() });
      await queryClient.invalidateQueries({ queryKey: ["admin"] });
    },
  });

  const failure =
    intervene.error instanceof ApiError && typeof intervene.error.detail === "string"
      ? intervene.error.detail
      : intervene.error?.message;

  return (
    <Card>
      <CardHeader
        title="Open work orders"
        subtitle={orders.data ? `${open.length} not yet finished, every district` : "Every district"}
      />
      {done && <p className="border-b border-hairline px-5 py-3 text-sm text-status-good-text">{done}</p>}

      {orders.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : orders.error ? (
        <ErrorState error={orders.error} />
      ) : open.length === 0 ? (
        <EmptyState title="No open work orders" hint="Every order is completed, failed or cancelled." />
      ) : (
        <ul className="divide-y divide-hairline">
          {open.map((order) => (
            <OrderRow
              key={order.order_id}
              order={order}
              acting={acting?.id === order.order_id ? acting.action : null}
              onStart={(action) => {
                setActing({ id: order.order_id, action });
                setReason("");
                setDone(null);
                intervene.reset();
              }}
              reason={reason}
              onReason={setReason}
              busy={intervene.isPending}
              failure={acting?.id === order.order_id ? failure : undefined}
              onConfirm={() => intervene.mutate()}
              onCancel={() => setActing(null)}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function OrderRow({
  order,
  acting,
  onStart,
  reason,
  onReason,
  busy,
  failure,
  onConfirm,
  onCancel,
}: {
  order: WorkOrder;
  acting: "release" | "cancel" | null;
  onStart: (action: "release" | "cancel") => void;
  reason: string;
  onReason: (v: string) => void;
  busy: boolean;
  failure: string | undefined;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const live = order.assignments.filter((a) => a.status === "offered" || a.status === "accepted");
  const canRelease = live.length > 0 || order.status !== "draft";

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink">
            {ORDER_TYPE_LABEL[order.order_type]}
            <span className="font-normal text-ink-muted"> · {order.site_label}, {order.district}</span>
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            Raised {formatWhen(order.created_at)}
            {" · "}
            {live.length === 0
              ? "nobody assigned"
              : live.map((a) => `${a.worker_name} (${a.job_role}, ${a.status})`).join(", ")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={ORDER_STATUS_TONE[order.status]}>{order.status.replace("_", " ")}</Badge>
          {canRelease && (
            <button
              type="button"
              onClick={() => onStart("release")}
              aria-pressed={acting === "release"}
              className="rounded-md border border-hairline px-3 py-1.5 text-sm text-ink-2 hover:bg-plane"
            >
              Release
            </button>
          )}
          <button
            type="button"
            onClick={() => onStart("cancel")}
            aria-pressed={acting === "cancel"}
            className="rounded-md border border-hairline px-3 py-1.5 text-sm text-ink-2 hover:bg-plane"
          >
            Cancel order
          </button>
        </div>
      </div>

      {acting && (
        <form
          className="mt-3 space-y-2 rounded-lg border border-hairline p-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (reason.trim().length >= 3) onConfirm();
          }}
        >
          <p className="text-xs text-ink-2">
            {acting === "release"
              ? "Everyone on this job is taken off it and it goes back to the dispatcher's queue."
              : "The order ends. Everyone on it, the dispatcher and the household are told. This cannot be undone."}
          </p>
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Reason (sent to the people involved and kept in the audit log)
            <input
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
            />
          </label>
          {failure && <p className="text-xs text-status-critical">{failure}</p>}
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={reason.trim().length < 3 || busy}
              className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy ? "Working…" : acting === "release" ? "Release" : "Cancel order"}
            </button>
            <button type="button" onClick={onCancel} className="text-sm text-ink-muted underline">
              Keep as is
            </button>
          </div>
        </form>
      )}
    </li>
  );
}
