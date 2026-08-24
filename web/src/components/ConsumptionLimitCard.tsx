/**
 * Consumer requirement 5, settings half.
 *
 * The jobs runner has read `site_consumption_limit` every morning since it
 * landed and warns the household when month-to-date import crosses the
 * threshold; this is where the household sets the figure it is measured
 * against.
 *
 * Two decisions worth stating:
 *
 * The progress bar is not the only signal. `status-warning`'s amber is
 * sub-3:1 at a hairline's width, and the codebase's rule is that status never
 * travels by colour alone -- so the percentage is spelled out in text and the
 * bar reinforces it rather than carrying it.
 *
 * The kWh figure is kept as the string the API sent it as, all the way to the
 * input's value. Money and energy are NUMERIC on the server and `string` on
 * the wire (rule 5); parsing one into a JavaScript number so a form could hold
 * it would reintroduce exactly the precision loss the rule exists to prevent.
 */
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, formatKwh, queryKeys, toNumber } from "../lib/api";
import { Card, CardHeader, ErrorState, Skeleton } from "../components/ui";

export default function ConsumptionLimitCard({ siteId }: { siteId: string }) {
  const queryClient = useQueryClient();
  const limit = useQuery({
    queryKey: queryKeys.consumptionLimit(siteId),
    queryFn: () => api.consumptionLimit(siteId),
  });

  const [editing, setEditing] = useState(false);
  const [monthlyKwh, setMonthlyKwh] = useState("");
  const [notifyPct, setNotifyPct] = useState("80");

  // Seed the form from the server's copy whenever it arrives or changes --
  // including after a save, so the input shows what was actually stored rather
  // than what was typed.
  useEffect(() => {
    if (!limit.data) return;
    setMonthlyKwh(limit.data.monthly_kwh ?? "");
    setNotifyPct(limit.data.notify_at_pct ?? "80");
  }, [limit.data]);

  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: queryKeys.consumptionLimit(siteId),
    });

  const save = useMutation({
    mutationFn: () =>
      api.setConsumptionLimit(siteId, {
        monthly_kwh: monthlyKwh.trim(),
        notify_at_pct: notifyPct.trim(),
      }),
    onSuccess: async () => {
      await invalidate();
      setEditing(false);
    },
  });

  const clear = useMutation({
    mutationFn: () => api.clearConsumptionLimit(siteId),
    onSuccess: async () => {
      await invalidate();
      setEditing(false);
    },
  });

  if (limit.isPending) {
    return (
      <Card>
        <CardHeader title="Monthly limit" />
        <div className="space-y-3 p-5">
          <Skeleton className="h-3 w-40" />
          <Skeleton className="h-2 w-full" />
        </div>
      </Card>
    );
  }

  if (limit.error) {
    return (
      <Card>
        <CardHeader title="Monthly limit" />
        <ErrorState error={limit.error} />
      </Card>
    );
  }

  const data = limit.data;
  const hasLimit = data.monthly_kwh !== null;
  const pct = hasLimit
    ? (toNumber(data.used_kwh) / toNumber(data.monthly_kwh!)) * 100
    : 0;
  const threshold = hasLimit ? toNumber(data.notify_at_pct!) : 80;
  const over = hasLimit && pct >= threshold;
  const month = new Date(`${data.month_start}T00:00:00`).toLocaleDateString(
    undefined,
    { month: "long", year: "numeric" },
  );

  return (
    <Card>
      <CardHeader
        title="Monthly limit"
        subtitle={`${formatKwh(data.used_kwh, 1)} kWh used in ${month}`}
        action={
          !editing ? (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="text-sm font-medium text-ink-2 underline"
            >
              {hasLimit ? "Change" : "Set a limit"}
            </button>
          ) : undefined
        }
      />

      <div className="space-y-4 p-5">
        {hasLimit && !editing && (
          <>
            <div className="flex items-baseline justify-between gap-3">
              <p className="text-sm text-ink">
                <span className="tabular font-medium">{pct.toFixed(0)}%</span> of{" "}
                <span className="tabular">
                  {formatKwh(data.monthly_kwh!, 0)}
                </span>{" "}
                kWh
              </p>
              <p className="text-xs text-ink-muted">
                Warn at {toNumber(data.notify_at_pct!).toFixed(0)}%
              </p>
            </div>

            {/* The bar reinforces the number above; it never carries the
                status on its own. */}
            <div
              className="h-2 w-full overflow-hidden rounded-full bg-hairline"
              role="img"
              aria-label={`${pct.toFixed(0)} percent of the monthly limit used`}
            >
              <div
                className={`h-full rounded-full ${
                  over ? "bg-status-critical" : "bg-ink-2"
                }`}
                style={{ width: `${Math.min(pct, 100)}%` }}
              />
            </div>

            <p className="text-xs text-ink-muted">
              {over
                ? "You are over your warning threshold for this month."
                : `That is about ${formatKwh(
                    data.daily_allowance_kwh!,
                    1,
                  )} kWh a day.`}{" "}
              We will send you one notification when you cross{" "}
              {toNumber(data.notify_at_pct!).toFixed(0)}%.
            </p>
          </>
        )}

        {!hasLimit && !editing && (
          <p className="text-sm text-ink-muted">
            No limit set. Set one and you will get a single notification in the
            month your usage crosses it.
          </p>
        )}

        {editing && (
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-sm">
                <span className="font-medium text-ink-2">
                  Monthly limit (kWh)
                </span>
                <input
                  type="number"
                  min="1"
                  max="100000"
                  step="1"
                  required
                  value={monthlyKwh}
                  onChange={(e) => setMonthlyKwh(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
              </label>
              <label className="block text-sm">
                <span className="font-medium text-ink-2">Warn me at (%)</span>
                <input
                  type="number"
                  min="1"
                  max="100"
                  step="1"
                  required
                  value={notifyPct}
                  onChange={(e) => setNotifyPct(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
              </label>
            </div>

            {(save.error || clear.error) && (
              <p className="text-sm text-status-critical">
                {String((save.error ?? clear.error)?.message ?? "Could not save")}
              </p>
            )}

            <div className="flex flex-wrap items-center gap-3">
              <button
                type="submit"
                disabled={save.isPending || !monthlyKwh.trim()}
                className="rounded-lg bg-ink px-4 py-2 text-sm font-medium text-surface disabled:opacity-50"
              >
                {save.isPending ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                onClick={() => setEditing(false)}
                className="text-sm text-ink-muted underline"
              >
                Cancel
              </button>
              {hasLimit && (
                <button
                  type="button"
                  onClick={() => clear.mutate()}
                  disabled={clear.isPending}
                  className="ml-auto text-sm text-ink-muted underline disabled:opacity-50"
                >
                  {clear.isPending ? "Removing…" : "Remove limit"}
                </button>
              )}
            </div>
          </form>
        )}
      </div>
    </Card>
  );
}
