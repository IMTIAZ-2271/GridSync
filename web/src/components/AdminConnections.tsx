import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, formatKwh, formatMoney, queryKeys, type AdminAccountDetail, type AdminConnection } from "../lib/api";
import { formatWhen } from "../lib/admin";

const DECIMAL = /^-?\d{1,8}(\.\d{1,4})?$/;

/**
 * An account's connections, each with its credit balance, ledger and the admin
 * credit adjustment.
 *
 * An adjustment is a new ledger row (rule 1), never an edit, and it cannot take
 * a balance below zero. Figures are typed as exact decimal strings and sent
 * that way; nothing here passes money through a float.
 */
export default function AdminConnections({ account }: { account: AdminAccountDetail }) {
  const connections = account.sites.flatMap((s) =>
    s.connections.map((c) => ({ ...c, site: s.label })),
  );
  if (connections.length === 0) return null;

  return (
    <div className="border-t border-hairline px-5 py-4">
      <p className="text-xs font-medium tracking-wide text-ink-muted uppercase">Connections</p>
      <ul className="mt-2 space-y-3">
        {connections.map((c) => (
          <ConnectionItem key={c.point_id} connection={c} site={c.site} accountId={account.account_id} />
        ))}
      </ul>
    </div>
  );
}

function ConnectionItem({
  connection,
  site,
  accountId,
}: {
  connection: AdminConnection;
  site: string;
  accountId: string;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<"adjust" | "ledger" | null>(null);
  const [kwh, setKwh] = useState("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [done, setDone] = useState<string | null>(null);

  const ledger = useQuery({
    queryKey: queryKeys.adminPointLedger(connection.point_id),
    queryFn: () => api.adminPointLedger(connection.point_id),
    enabled: open === "ledger",
  });

  const adjust = useMutation({
    mutationFn: () =>
      api.adminAdjustCredit(connection.point_id, {
        kwh_delta: kwh.trim() || "0",
        amount_delta: amount.trim() || "0",
        reason: reason.trim(),
      }),
    onSuccess: (result) => {
      setDone(`Adjusted. Balance is now ${formatKwh(result.balance_kwh, 4)} kWh, ${formatMoney(result.balance_amount)}.`);
      setKwh("");
      setAmount("");
      setReason("");
      setOpen(null);
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminAccount(accountId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminPointLedger(connection.point_id) });
      await queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  });

  const kwhOk = kwh.trim() === "" || DECIMAL.test(kwh.trim());
  const amountOk = amount.trim() === "" || DECIMAL.test(amount.trim());
  const moves = Number(kwh || 0) !== 0 || Number(amount || 0) !== 0;
  const valid = kwhOk && amountOk && moves && reason.trim().length >= 3;
  const failure =
    adjust.error instanceof ApiError && typeof adjust.error.detail === "string"
      ? adjust.error.detail
      : adjust.error?.message;

  return (
    <li className="rounded-lg border border-hairline p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-medium text-ink">
          {connection.label}
          <span className="font-normal text-ink-muted"> · {site}</span>
        </p>
        <p className="tabular text-sm text-ink">
          {formatKwh(connection.balance_kwh, 4)} kWh
          <span className="text-ink-muted" title={`BDT ${connection.balance_amount}`}> · {formatMoney(connection.balance_amount)}</span>
        </p>
      </div>
      <p className="mt-0.5 font-mono text-xs text-ink-muted">
        {connection.meter_serial ?? "no billing meter"}
        {connection.reference && ` · ${connection.reference}`}
      </p>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          aria-pressed={open === "adjust"}
          onClick={() => {
            setOpen(open === "adjust" ? null : "adjust");
            setDone(null);
            adjust.reset();
          }}
          className="rounded-md border border-hairline px-2.5 py-1 text-xs text-ink-2 hover:bg-plane"
        >
          Adjust credit
        </button>
        <button
          type="button"
          aria-pressed={open === "ledger"}
          onClick={() => setOpen(open === "ledger" ? null : "ledger")}
          className="rounded-md border border-hairline px-2.5 py-1 text-xs text-ink-2 hover:bg-plane"
        >
          {open === "ledger" ? "Hide ledger" : "Ledger"}
        </button>
      </div>
      {done && <p className="mt-2 text-xs text-status-good-text">{done}</p>}

      {open === "adjust" && (
        <form
          className="mt-3 space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) adjust.mutate();
          }}
        >
          <p className="text-xs text-ink-2">
            Positive adds credit, negative removes it. Written as a new ledger entry; the balance
            cannot go below zero.
          </p>
          <div className="grid grid-cols-2 gap-2">
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              kWh
              <input
                inputMode="decimal"
                value={kwh}
                onChange={(e) => setKwh(e.target.value)}
                placeholder="-12.5000"
                aria-invalid={!kwhOk}
                className="rounded-md border border-hairline bg-surface px-2 py-1.5 font-mono text-sm text-ink"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-ink-muted">
              Amount (BDT)
              <input
                inputMode="decimal"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="-78.1250"
                aria-invalid={!amountOk}
                className="rounded-md border border-hairline bg-surface px-2 py-1.5 font-mono text-sm text-ink"
              />
            </label>
          </div>
          {(!kwhOk || !amountOk) && (
            <p className="text-xs text-status-critical">Use a number with at most 4 decimal places.</p>
          )}
          <label className="flex flex-col gap-1 text-xs text-ink-muted">
            Reason (kept on the ledger entry and in the audit log)
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
            />
          </label>
          {failure && <p className="text-xs text-status-critical">{failure}</p>}
          <button
            type="submit"
            disabled={!valid || adjust.isPending}
            className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
          >
            {adjust.isPending ? "Working…" : "Post adjustment"}
          </button>
        </form>
      )}

      {open === "ledger" && (
        <div className="mt-3 overflow-x-auto">
          {ledger.isPending ? (
            <p className="text-xs text-ink-muted">Loading…</p>
          ) : ledger.error ? (
            <p className="text-xs text-status-critical">{ledger.error.message}</p>
          ) : ledger.data.entries.length === 0 ? (
            <p className="text-xs text-ink-muted">No ledger entries yet.</p>
          ) : (
            <table className="w-full min-w-[28rem] text-xs">
              <thead>
                <tr className="text-left text-ink-muted">
                  <th className="py-1 pr-2 font-medium">When</th>
                  <th className="py-1 pr-2 font-medium">Type</th>
                  <th className="py-1 pr-2 text-right font-medium">kWh</th>
                  <th className="py-1 pr-2 text-right font-medium">Balance</th>
                  <th className="py-1 font-medium">Note</th>
                </tr>
              </thead>
              <tbody>
                {ledger.data.entries.map((e) => (
                  <tr key={e.entry_id} className="border-t border-hairline align-top">
                    <td className="py-1 pr-2 whitespace-nowrap text-ink-2">{formatWhen(e.created_at)}</td>
                    <td className="py-1 pr-2 text-ink-2">{e.entry_type}</td>
                    <td className="tabular py-1 pr-2 text-right font-mono text-ink">{e.kwh_delta}</td>
                    <td className="tabular py-1 pr-2 text-right font-mono text-ink">{e.balance_kwh_after}</td>
                    <td className="py-1 text-ink-2">{e.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </li>
  );
}
