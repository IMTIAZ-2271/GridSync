import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, queryKeys, type MeterConnection } from "../lib/api";
import { CONNECTION, connectionHint } from "../lib/commissioning";
import { Badge, Card, CardHeader, ErrorState, Skeleton } from "./ui";

/**
 * The official's list of billing meters in their district that are not getting
 * readings from their utility's network, with a way to try again.
 *
 * Officials own this because they register meters and are who a failed
 * connection notifies. Only meters needing attention are listed -- a connected
 * meter needs nothing from anyone -- and the card renders nothing at all where
 * the server does not commission meters.
 *
 * Retry is one click with no confirmation: it offers the meter again, and the
 * worst it can do is fail again. Invalidate rather than patch, because the
 * server decides the new state.
 */
export default function MeterConnections() {
  const queryClient = useQueryClient();
  const overview = useQuery({
    queryKey: queryKeys.commissioning(),
    queryFn: api.commissioning,
  });

  const retry = useMutation({
    mutationFn: (deviceId: string) => api.retryCommissioning(deviceId),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.commissioning() }),
  });

  if (overview.data && !overview.data.enabled) return null;

  const meters = overview.data?.meters ?? [];
  const attention = meters.filter((m) => m.needs_attention);
  const connected = meters.filter((m) => m.state === "live").length;

  return (
    <Card>
      <CardHeader
        title="Meter connections"
        subtitle={
          overview.data
            ? `${connected} of ${meters.length} billing meters in your district send readings through their utility`
            : "Billing meters in your district"
        }
      />
      {overview.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-10 w-full" />
        </div>
      ) : overview.error ? (
        <ErrorState error={overview.error} />
      ) : attention.length === 0 ? (
        <p className="px-5 py-4 text-sm text-ink-2">
          Every billing meter is connected or waiting to be claimed.
        </p>
      ) : (
        <ul className="divide-y divide-hairline">
          {attention.map((meter) => (
            <ConnectionRow
              key={meter.device_id}
              meter={meter}
              busy={retry.isPending && retry.variables === meter.device_id}
              error={
                retry.variables === meter.device_id ? retry.error : null
              }
              onRetry={() => retry.mutate(meter.device_id)}
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

function ConnectionRow({
  meter,
  busy,
  error,
  onRetry,
}: {
  meter: MeterConnection;
  busy: boolean;
  error: Error | null;
  onRetry: () => void;
}) {
  const state = CONNECTION[meter.state];
  return (
    <li className="flex flex-wrap items-start justify-between gap-3 px-5 py-4">
      <div className="min-w-0">
        <p className="text-sm font-medium text-ink">
          {meter.site_label}
          <span className="font-normal text-ink-muted"> · {meter.point_label}</span>
        </p>
        <p className="mt-0.5 font-mono text-xs text-ink-muted">{meter.serial_no}</p>
        <p className="mt-1 text-xs text-ink-2">{connectionHint(meter)}</p>
        {error && (
          <p className="mt-1 text-xs text-status-critical">
            {error instanceof ApiError && typeof error.detail === "string"
              ? error.detail
              : error.message}
          </p>
        )}
      </div>
      <div className="flex items-center gap-3">
        <Badge tone={state.tone}>{state.label}</Badge>
        <button
          type="button"
          disabled={busy}
          onClick={onRetry}
          className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
        >
          {busy ? "Retrying…" : "Retry connection"}
        </button>
      </div>
    </li>
  );
}
