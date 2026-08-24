import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  formatKwh,
  queryKeys,
  type ApplicationStatus,
  type SolarApplication,
} from "../lib/api";
import {
  APPLICATION_STATUS_LABEL,
  APPLICATION_STATUS_TONE,
  isOpenApplication,
} from "../lib/applications";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Supplier requirement 1: the installer's inbox.
 *
 * The same rows the household sees on its own page, from the other end. Open
 * applications sort first and oldest-first within that — a queue sorted purely
 * by recency buries whoever nobody answered, which is the same rule the worker
 * triage queue and the official's approval queue follow.
 *
 * The moves offered per row are exactly what the API allows, and `completed` is
 * reachable only from `accepted`: a job nobody agreed to cannot have been
 * finished. Rejecting asks for a reason on the row — the household is told what
 * the installer said, and "not accepted" on its own tells them nothing they can
 * act on.
 */

/** What this row can become next, and what the button should say. */
const MOVES: Partial<
  Record<ApplicationStatus, { to: ApplicationStatus; label: string }[]>
> = {
  submitted: [
    { to: "under_review", label: "Start reviewing" },
    { to: "accepted", label: "Accept" },
  ],
  under_review: [{ to: "accepted", label: "Accept" }],
  accepted: [{ to: "completed", label: "Mark installed" }],
};

export default function SupplierApplications() {
  const queryClient = useQueryClient();
  const [openOnly, setOpenOnly] = useState(true);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const applications = useQuery({
    queryKey: queryKeys.solarApplications(openOnly),
    queryFn: () => api.solarApplications(openOnly),
  });

  const decide = useMutation({
    mutationFn: ({
      id,
      status,
      notes,
    }: {
      id: string;
      status: ApplicationStatus;
      notes?: string;
    }) => api.decideSolarApplication(id, status, notes),
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["solar-applications"] });
      setRejecting(null);
      setReason("");
    },
  });

  const conflict =
    decide.error instanceof ApiError && decide.error.status === 409;
  const rows = applications.data ?? [];
  const waiting = rows.filter((a) => isOpenApplication(a.status)).length;

  return (
    <Card>
      <CardHeader
        title="Solar applications"
        subtitle="Households asking your firm to fit panels"
        action={
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-2 text-xs text-ink-2">
              <input
                type="checkbox"
                checked={openOnly}
                onChange={(e) => setOpenOnly(e.target.checked)}
              />
              Open only
            </label>
            {applications.data && (
              <Badge tone={waiting > 0 ? "warning" : "good"}>
                {waiting} waiting
              </Badge>
            )}
          </div>
        }
      />

      {conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-ink-2">
          Someone else decided that application first. The queue has been
          refreshed.
        </p>
      )}
      {decide.error && !conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-status-critical">
          {decide.error instanceof ApiError
            ? String(decide.error.detail)
            : decide.error.message}
        </p>
      )}

      {applications.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-20 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : applications.error ? (
        <ErrorState error={applications.error} />
      ) : rows.length === 0 ? (
        <EmptyState
          title={openOnly ? "Nothing waiting" : "No applications yet"}
          hint="Households pick an installer that works in their district, so applications only reach firms with a service area covering the site."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {rows.map((app) => (
            <ApplicationRow
              key={app.application_id}
              app={app}
              busy={decide.isPending && decide.variables?.id === app.application_id}
              rejecting={rejecting === app.application_id}
              reason={reason}
              onReason={setReason}
              onMove={(status) =>
                decide.mutate({ id: app.application_id, status })
              }
              onStartReject={() => {
                setRejecting(app.application_id);
                setReason("");
              }}
              onCancelReject={() => setRejecting(null)}
              onConfirmReject={() =>
                decide.mutate({
                  id: app.application_id,
                  status: "rejected",
                  notes: reason.trim() || undefined,
                })
              }
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function ApplicationRow({
  app,
  busy,
  rejecting,
  reason,
  onReason,
  onMove,
  onStartReject,
  onCancelReject,
  onConfirmReject,
}: {
  app: SolarApplication;
  busy: boolean;
  rejecting: boolean;
  reason: string;
  onReason: (v: string) => void;
  onMove: (status: ApplicationStatus) => void;
  onStartReject: () => void;
  onCancelReject: () => void;
  onConfirmReject: () => void;
}) {
  const moves = MOVES[app.status] ?? [];
  const waited = Math.floor(
    (Date.now() - +new Date(app.submitted_at)) / 86_400_000,
  );

  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="font-medium text-ink">
            {formatKwh(app.requested_capacity_kw, 1)} kW · {app.site_label}
            {app.panel_count ? ` · ${app.panel_count} panels` : ""}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            {app.address_line ? `${app.address_line} · ` : ""}
            {app.district} · {app.point_label} · {app.account_name}
            {app.account_phone && ` · ${app.account_phone}`}
          </p>
          <p className="mt-0.5 text-xs text-ink-muted">
            Applied{" "}
            {waited === 0
              ? "today"
              : `${waited} day${waited === 1 ? "" : "s"} ago`}
          </p>
          {app.notes && <p className="mt-1 text-sm text-ink-2">{app.notes}</p>}
          {/* Not a warning, just a fact the quote depends on: an uprate is real
              work, but it is not the same job as a first installation. */}
          {app.site_has_solar && (
            <p className="mt-1 text-xs text-ink-2">
              This site already has a live array — this would be an uprate.
            </p>
          )}
          {app.decision_notes && (
            <p className="mt-1 text-sm text-ink-2">
              Decision: {app.decision_notes}
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Badge tone={APPLICATION_STATUS_TONE[app.status]}>
            {APPLICATION_STATUS_LABEL[app.status]}
          </Badge>
        </div>
      </div>

      {!rejecting && moves.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          {moves.map((m) => (
            <button
              key={m.to}
              type="button"
              disabled={busy}
              onClick={() => onMove(m.to)}
              className={
                m.to === "accepted" || m.to === "completed"
                  ? "rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
                  : "rounded-lg border border-hairline px-3 py-1.5 text-sm font-medium text-ink-2 disabled:opacity-50"
              }
            >
              {busy ? "Saving…" : m.label}
            </button>
          ))}
          {isOpenApplication(app.status) && (
            <button
              type="button"
              disabled={busy}
              onClick={onStartReject}
              className="ml-auto text-sm text-ink-muted underline disabled:opacity-50"
            >
              Cannot take this on
            </button>
          )}
        </div>
      )}

      {rejecting && (
        <div className="mt-3 space-y-2 rounded-lg border border-hairline p-3">
          <label className="block text-sm">
            <span className="font-medium text-ink-2">Why not?</span>
            <input
              type="text"
              autoFocus
              value={reason}
              onChange={(e) => onReason(e.target.value)}
              placeholder="Roof needs structural work first"
              className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
            />
          </label>
          <p className="text-xs text-ink-muted">
            This is sent to the household. "Not accepted" on its own tells them
            nothing they can act on.
          </p>
          <div className="flex items-center gap-3">
            <button
              type="button"
              disabled={busy}
              onClick={onConfirmReject}
              className="rounded-lg bg-status-critical px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
            >
              {busy ? "Sending…" : "Send decision"}
            </button>
            <button
              type="button"
              onClick={onCancelReject}
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
