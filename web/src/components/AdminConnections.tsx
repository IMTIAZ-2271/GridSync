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
  const [open, setOpen] = useState<"adjust" | "ledger" | "bills" | null>(null);
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
        <button
          type="button"
          aria-pressed={open === "bills"}
          onClick={() => setOpen(open === "bills" ? null : "bills")}
          className="rounded-md border border-hairline px-2.5 py-1 text-xs text-ink-2 hover:bg-plane"
        >
          {open === "bills" ? "Hide bills" : "Bills"}
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

      {open === "bills" && (
        <ConnectionBills pointId={connection.point_id} accountId={accountId} />
      )}
    </li>
  );
}

/**
 * The connection's bills, newest month first, with a reissue on the one bill
 * that can take it: not void, the latest, no payments. A reissue voids that
 * bill and issues a corrected replacement for the same month; the server
 * re-checks every guard, and says which one refused.
 */
function ConnectionBills({ pointId, accountId }: { pointId: string; accountId: string }) {
  const queryClient = useQueryClient();
  const [reissuing, setReissuing] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [mergeLate, setMergeLate] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const bills = useQuery({
    queryKey: queryKeys.adminPointBills(pointId),
    queryFn: () => api.adminPointBills(pointId),
  });

  const reissue = useMutation({
    mutationFn: () =>
      api.adminReissueBill(reissuing!, { reason: reason.trim(), merge_late_readings: mergeLate }),
    onSuccess: (r) => {
      setDone(
        `Reissued. Charges ${formatMoney(r.previous_gross_amount)} → ${formatMoney(r.gross_amount)}; ` +
          `credit used ${formatKwh(r.previous_credit_applied_kwh, 4)} → ${formatKwh(r.credit_applied_kwh, 4)} kWh; ` +
          `amount due ${formatMoney(r.previous_amount_due)} → ${formatMoney(r.amount_due)}. The household has been told.`,
      );
      setReissuing(null);
      setReason("");
      setMergeLate(false);
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminPointBills(pointId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminPointLedger(pointId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.adminAccount(accountId) });
      await queryClient.invalidateQueries({ queryKey: ["admin", "audit"] });
    },
  });
  const failure =
    reissue.error instanceof ApiError && typeof reissue.error.detail === "string"
      ? reissue.error.detail
      : reissue.error?.message;

  if (bills.isPending) return <p className="mt-3 text-xs text-ink-muted">Loading…</p>;
  if (bills.error) return <p className="mt-3 text-xs text-status-critical">{bills.error.message}</p>;
  if (bills.data.length === 0) return <p className="mt-3 text-xs text-ink-muted">No bills yet.</p>;

  return (
    <div className="mt-3 space-y-2">
      {done && <p className="text-xs text-status-good-text">{done}</p>}
      <ul className="divide-y divide-hairline rounded-md border border-hairline">
        {bills.data.map((b) => (
          <li key={b.bill_id} className="px-3 py-2">
            <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="text-ink">
                {new Date(b.period_start + "T00:00:00").toLocaleDateString(undefined, {
                  month: "long",
                  year: "numeric",
                })}
                <span className="text-ink-muted">
                  {" · "}charges {formatMoney(b.gross_amount)} · due {formatMoney(b.amount_due)}
                </span>
              </span>
              <span className="flex items-center gap-2">
                <span
                  className={`rounded px-1.5 py-0.5 ${
                    b.status === "void" ? "bg-hairline text-ink-muted" : "bg-status-good/12 text-status-good-text"
                  }`}
                >
                  {b.status === "void" ? "void — replaced" : b.status}
                </span>
                {b.reissuable && (
                  <button
                    type="button"
                    aria-pressed={reissuing === b.bill_id}
                    onClick={() => {
                      setReissuing(reissuing === b.bill_id ? null : b.bill_id);
                      setDone(null);
                      reissue.reset();
                    }}
                    className="rounded-md border border-hairline px-2 py-0.5 text-ink-2 hover:bg-plane"
                  >
                    Reissue
                  </button>
                )}
              </span>
            </div>
            {reissuing === b.bill_id && (
              <form
                className="mt-2 space-y-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (reason.trim().length >= 3) reissue.mutate();
                }}
              >
                <p className="text-xs text-ink-2">
                  This bill is voided and a corrected one is issued for the same month from the
                  readings and tariff as they are now. Its credit is reversed and applied again. The
                  household is told. This cannot be undone.
                </p>
                <label className="flex items-center gap-1.5 text-xs text-ink-2">
                  <input
                    type="checkbox"
                    checked={mergeLate}
                    onChange={(e) => setMergeLate(e.target.checked)}
                  />
                  Include readings that arrived after the month was billed
                </label>
                <label className="flex flex-col gap-1 text-xs text-ink-muted">
                  Reason (sent to the household and kept in the audit log)
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="rounded-md border border-hairline bg-surface px-2 py-1.5 text-sm text-ink"
                  />
                </label>
                {failure && <p className="text-xs text-status-critical">{failure}</p>}
                <button
                  type="submit"
                  disabled={reason.trim().length < 3 || reissue.isPending}
                  className="rounded-lg bg-ink px-3 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
                >
                  {reissue.isPending ? "Reissuing…" : "Void and reissue"}
                </button>
              </form>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
