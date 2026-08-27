/**
 * Where an application has actually got to.
 *
 * Since 2026-08-27 neither a meter nor a net-metering agreement is granted on
 * a click. Both run through a visit:
 *
 *   apply -> official orders the work -> a worker does it and records the
 *   serial -> the household confirms -> the official registers the meter ->
 *   the household installs it and readings begin
 *
 * That means the honest answer to "where is my application?" is not the
 * application's own status. It is the status *plus* the visit's, and the
 * household's verdict on it. Deriving it here rather than storing a stage
 * column is deliberate: a stored stage is a second place for the same fact to
 * live, and the one that goes stale.
 *
 * Both portals import this, so the household and the official cannot describe
 * the same row differently.
 */
import type { ApplicationVisit } from "./api";

export type StageTone = "neutral" | "info" | "good" | "warning" | "critical";

export interface Stage {
  /** Short label for a badge. */
  label: string;
  tone: StageTone;
  /** One sentence: what is happening, or what the reader should do. */
  detail: string;
  /** True when the ball is in the household's court. */
  awaitingConsumer?: boolean;
  /** True when the ball is in the official's court. */
  awaitingOfficial?: boolean;
}

/**
 * The visit half, shared by both flows. `settled` short-circuits it: an
 * application that has been registered, refused or withdrawn is not described
 * by whatever its last visit was doing.
 */
export function visitStage(visit: ApplicationVisit | null): Stage {
  if (!visit) {
    return {
      label: "Submitted",
      tone: "info",
      detail:
        "Your district office has been notified. They will order a visit.",
      awaitingOfficial: true,
    };
  }

  switch (visit.status) {
    case "draft":
      return {
        label: "Visit ordered",
        tone: "info",
        detail:
          "A visit has been ordered and is waiting for a technician to be assigned.",
        awaitingOfficial: true,
      };
    case "scheduled":
      return {
        label: "Scheduled",
        tone: "info",
        detail: "A visit has been scheduled.",
      };
    case "dispatched":
      return {
        label: "Technician assigned",
        tone: "info",
        detail: "A technician has been assigned and is due to attend.",
      };
    case "in_progress":
      return {
        label: "Work in progress",
        tone: "info",
        detail: "A technician is on site now.",
      };
    case "cancelled":
      return {
        label: "Visit cancelled",
        tone: "warning",
        detail:
          "The visit was cancelled. Your district office will decide what happens next.",
        awaitingOfficial: true,
      };
    case "failed":
      return {
        label: "Could not be completed",
        tone: "critical",
        detail:
          visit.failure_reason ??
          "The technician could not finish the work. Your district office has been notified.",
        awaitingOfficial: true,
      };
    case "completed":
      if (visit.consumer_disputed_at) {
        return {
          label: "Disputed",
          tone: "warning",
          detail:
            "You reported that the work is not done. Your district office has been notified.",
          awaitingOfficial: true,
        };
      }
      if (!visit.consumer_confirmed_at) {
        return {
          label: "Confirm the work",
          tone: "warning",
          detail:
            "The technician has marked the work complete. Confirm it and your district office will register the meter.",
          awaitingConsumer: true,
        };
      }
      return {
        label: "Awaiting registration",
        tone: "info",
        detail:
          "You have confirmed the work. Your district office will register the meter to you.",
        awaitingOfficial: true,
      };
    default:
      return { label: visit.status, tone: "neutral", detail: "" };
  }
}

/** The meter flow, end to end. */
export function meterApplicationStage(
  status: string,
  visit: ApplicationVisit | null,
  meterStillAvailable: boolean | null,
): Stage {
  if (status === "accepted") {
    return meterStillAvailable
      ? {
          label: "Meter registered",
          tone: "good",
          detail:
            "The meter is registered to you. Install it on a connection from the Meters page and your readings begin.",
          awaitingConsumer: true,
        }
      : {
          label: "Done",
          tone: "good",
          detail: "The meter is registered and installed on a connection.",
        };
  }
  if (status === "rejected") {
    return {
      label: "Not approved",
      tone: "critical",
      detail: "Your district office did not approve this application.",
    };
  }
  if (status === "withdrawn") {
    return {
      label: "Withdrawn",
      tone: "neutral",
      detail: "You withdrew this application.",
    };
  }
  return visitStage(visit);
}

/** The net-metering flow, end to end. */
export function netMeteringStage(
  status: string,
  visit: ApplicationVisit | null,
): Stage {
  if (status === "active") {
    return {
      label: "Approved",
      tone: "good",
      detail:
        "Net metering is active on this connection. Your exports earn credit.",
    };
  }
  if (status === "suspended") {
    return {
      label: "Suspended",
      tone: "warning",
      detail: "This agreement is suspended. Contact your district office.",
    };
  }
  if (status === "terminated") {
    return {
      label: "Closed",
      tone: "neutral",
      detail: "This application is no longer open.",
    };
  }
  return visitStage(visit);
}

/**
 * What a household has to have in place, and what the scheme then gives them.
 *
 * Written from the rules the system actually applies, so the list cannot
 * promise something the API then refuses -- items 1 and 2 are 409s from
 * `POST /api/net-metering-applications`, item 3 is read off the installed
 * hardware rather than the request, and items 4 and 5 are the agreement's own
 * stored terms. Shown before applying and again after a failed inspection,
 * because those are the two moments a household needs it.
 */
export const NET_METERING_REQUIREMENTS: { title: string; body: string }[] = [
  {
    title: "The panels must already be installed and registered",
    body: "Net metering credits what your array exports, so the array has to exist first. Register it on the Meters page against the connection it feeds.",
  },
  {
    title: "The connection needs a billing meter",
    body: "Only the bidirectional meter at the grid boundary can tell import from export. The inspection replaces your existing meter with one that can.",
  },
  {
    title: "Capacity is taken from what is installed",
    body: "Your sanctioned capacity is read from the array on the connection, not from anything you type. It is what the inspection verifies.",
  },
  {
    title: "Export is capped at 70% of sanctioned capacity",
    body: "Anything above the cap is not credited. The cap is recorded on the agreement when it is approved.",
  },
  {
    title: "Credit rolls over for 12 months",
    body: "Unused export credit carries forward and is applied to later bills. It does not become a cash payment.",
  },
  {
    title: "One application per connection",
    body: "A site with two connections applies for each separately — each is billed on its own and keeps its own credit balance.",
  },
  {
    title: "Safe, reachable access to the meter position",
    body: "The technician has to reach the meter and the inverter to inspect and swap. A visit that cannot get access is recorded as failed.",
  },
];
