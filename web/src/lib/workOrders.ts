/**
 * Work order vocabulary, and the transitions the worker portal offers.
 *
 * The API accepts any `work_order_status` on PATCH -- there is no state
 * machine behind it, deliberately, because a dispatcher sometimes has to put
 * an order back where it belongs after a mistake. What follows is therefore a
 * convenience, not enforcement: it offers the move a field engineer actually
 * makes next, and does not pretend the others are impossible. Same posture as
 * RequireAuth -- the client decides what is easy, the server decides what is
 * allowed.
 */
import type { WorkOrderStatus, WorkOrderType } from "./api";

export const ORDER_TYPE_LABEL: Record<WorkOrderType, string> = {
  meter_install: "Meter install",
  meter_swap: "Meter swap",
  meter_removal: "Meter removal",
  inverter_service: "Inverter service",
  inspection: "Inspection",
  seal_check: "Seal check",
  disconnection: "Disconnection",
  reconnection: "Reconnection",
};

export const ORDER_STATUS_TONE: Record<WorkOrderStatus, string> = {
  draft: "neutral",
  scheduled: "neutral",
  dispatched: "warning",
  in_progress: "warning",
  completed: "good",
  failed: "critical",
  cancelled: "neutral",
};

/**
 * The three buckets a field engineer actually thinks in: what am I on now,
 * what is coming, what is finished. Not one tab per status -- seven tabs for
 * six orders would be filing, not triage.
 */
export type OrderBucket = "active" | "upcoming" | "closed";

export const BUCKETS: { id: OrderBucket; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "upcoming", label: "Upcoming" },
  { id: "closed", label: "Closed" },
];

export function bucketOf(status: WorkOrderStatus): OrderBucket {
  switch (status) {
    case "dispatched":
    case "in_progress":
      return "active";
    case "draft":
    case "scheduled":
      return "upcoming";
    default:
      return "closed";
  }
}

export interface Transition {
  to: WorkOrderStatus;
  label: string;
  /** Primary actions read as the expected next step; the rest are quieter. */
  emphasis: "primary" | "secondary";
}

/** What to offer from here. Empty once the order is closed. */
export function transitionsFrom(status: WorkOrderStatus): Transition[] {
  switch (status) {
    case "draft":
      return [
        { to: "scheduled", label: "Schedule", emphasis: "primary" },
        { to: "cancelled", label: "Cancel", emphasis: "secondary" },
      ];
    case "scheduled":
      return [
        { to: "dispatched", label: "Dispatch", emphasis: "primary" },
        { to: "cancelled", label: "Cancel", emphasis: "secondary" },
      ];
    case "dispatched":
      return [
        { to: "in_progress", label: "Start work", emphasis: "primary" },
        { to: "cancelled", label: "Cancel", emphasis: "secondary" },
      ];
    case "in_progress":
      return [
        { to: "completed", label: "Complete", emphasis: "primary" },
        { to: "failed", label: "Mark failed", emphasis: "secondary" },
      ];
    default:
      // completed / failed / cancelled. The order is done; reopening it is a
      // dispatcher's decision, not something to put a button on in a field
      // engineer's queue.
      return [];
  }
}
