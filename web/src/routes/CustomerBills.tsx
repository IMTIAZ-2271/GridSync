import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import {
  api,
  formatKwh,
  formatMoney,
  queryKeys,
  type Bill,
  type BillLineItem,
  type BillStatus,
} from "../lib/api";
import { SERIES } from "../lib/series";
import { useSelectedSite } from "../components/SitePicker";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

const STATUS_TONE: Record<BillStatus, string> = {
  issued: "neutral",
  partially_paid: "warning",
  paid: "good",
  overdue: "critical",
  void: "neutral",
};

const LINE_LABEL: Record<string, string> = {
  energy_import: "Energy imported",
  export_credit: "Export credit",
  fixed: "Fixed charge",
  demand: "Demand charge",
  tax: "Tax",
  adjustment: "Adjustment",
};

const TOU_LABEL: Record<string, string> = {
  peak: "Peak",
  shoulder: "Shoulder",
  off_peak: "Off-peak",
  flat: "Flat",
};

export default function CustomerBills() {
  const { siteId } = useSelectedSite();
  const [expanded, setExpanded] = useState<string | null>(null);

  const bills = useQuery({
    queryKey: queryKeys.siteBills(siteId!),
    queryFn: () => api.siteBills(siteId!),
    enabled: !!siteId,
  });

  return (
    <Card>
      <CardHeader
        title="Bills"
        subtitle="Newest period first. Select a row for its time-of-use breakdown."
      />

      {bills.isPending || !siteId ? (
        <div className="space-y-3 p-5">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : bills.error ? (
        <ErrorState error={bills.error} />
      ) : bills.data.length === 0 ? (
        <EmptyState
          title="No bills yet"
          hint="Bills appear once a billing period for this site has been closed and run."
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-hairline text-xs font-medium tracking-wide text-ink-muted uppercase">
                <th className="px-5 py-3 text-left">Period</th>
                <th className="px-3 py-3 text-right">Import</th>
                <th className="px-3 py-3 text-right">Export</th>
                <th className="px-3 py-3 text-right">Credit applied</th>
                <th className="px-3 py-3 text-right">Amount due</th>
                <th className="px-5 py-3 text-left">Status</th>
              </tr>
            </thead>
            <tbody>
              {bills.data.map((bill) => (
                <BillRow
                  key={bill.bill_id}
                  bill={bill}
                  // A household with one connection gets "Main" on every row,
                  // which distinguishes nothing. Only name the connection once
                  // there is more than one to tell apart.
                  showPoint={
                    new Set(bills.data.map((b) => b.billing_point_id)).size > 1
                  }
                  isOpen={expanded === bill.bill_id}
                  onToggle={() =>
                    setExpanded(expanded === bill.bill_id ? null : bill.bill_id)
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function BillRow({
  bill,
  showPoint,
  isOpen,
  onToggle,
}: {
  bill: Bill;
  showPoint: boolean;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const period = new Date(bill.period_start).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
  const settled = bill.amount_due === "0.0000";

  return (
    <Fragment>
      <tr
        onClick={onToggle}
        aria-expanded={isOpen}
        className={`cursor-pointer border-b border-hairline transition-colors last:border-0 hover:bg-plane ${
          isOpen ? "bg-plane" : ""
        }`}
      >
        <td className="px-5 py-3">
          <span className="flex items-center gap-2 font-medium text-ink">
            <span
              aria-hidden
              className={`text-ink-muted transition-transform ${isOpen ? "rotate-90" : ""}`}
            >
              ›
            </span>
            {period}
          </span>
          {/* Each billing point is billed independently and carries its own
              credit balance, so two bills for the same month are normal on a
              multi-meter site -- they are not duplicates. */}
          {showPoint && (
            <span className="mt-0.5 block pl-5 text-xs text-ink-2">
              {bill.point_label}
            </span>
          )}
          {/* Rule 8: a period billed on partial data must say so on its face. */}
          {bill.coverage_pct && Number(bill.coverage_pct) < 100 && (
            <span className="mt-0.5 block pl-5 text-xs text-ink-muted">
              {formatKwh(bill.coverage_pct, 1)}% interval coverage
            </span>
          )}
        </td>
        <td className="tabular px-3 py-3 text-right text-ink-2">
          {formatKwh(bill.total_import_kwh, 1)}
        </td>
        <td className="tabular px-3 py-3 text-right text-ink-2">
          {formatKwh(bill.total_export_kwh, 1)}
        </td>
        <td className="tabular px-3 py-3 text-right text-ink-2">
          {formatKwh(bill.credit_applied_kwh, 1)}
        </td>
        <td className="tabular px-3 py-3 text-right font-semibold text-ink">
          {formatKwh(bill.amount_due, 2)}
        </td>
        <td className="px-5 py-3">
          <Badge tone={settled ? "good" : STATUS_TONE[bill.status]}>
            {settled ? "covered by credit" : bill.status.replace(/_/g, " ")}
          </Badge>
        </td>
      </tr>

      {isOpen && (
        <tr className="border-b border-hairline bg-plane last:border-0">
          <td colSpan={6} className="px-5 py-4">
            <LineItems bill={bill} />
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function LineItems({ bill }: { bill: Bill }) {
  if (bill.line_items.length === 0) {
    return (
      <p className="text-xs text-ink-muted">
        This bill has no line items recorded.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      <div className="overflow-x-auto rounded-lg border border-hairline bg-surface">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-hairline text-ink-muted">
              <th className="px-4 py-2 text-left font-medium">Charge</th>
              <th className="px-3 py-2 text-left font-medium">Window</th>
              <th className="px-3 py-2 text-right font-medium">Quantity</th>
              <th className="px-3 py-2 text-right font-medium">Rate applied</th>
              <th className="px-4 py-2 text-right font-medium">Amount</th>
            </tr>
          </thead>
          <tbody>
            {bill.line_items.map((li) => (
              <LineItemRow key={li.sort_order} item={li} />
            ))}
          </tbody>
        </table>
      </div>

      <dl className="flex flex-wrap gap-x-8 gap-y-2 text-xs">
        <Figure label="Energy charge" value={formatMoney(bill.energy_charge, bill.currency)} />
        <Figure
          label="Export credit earned"
          value={formatMoney(bill.export_credit_earned, bill.currency)}
        />
        <Figure label="Fixed" value={formatMoney(bill.fixed_charge, bill.currency)} />
        <Figure label="Tax" value={formatMoney(bill.tax_amount, bill.currency)} />
        <Figure label="Gross" value={formatMoney(bill.gross_amount, bill.currency)} />
        <Figure
          label="Credit carried forward"
          value={`${formatKwh(bill.credit_closing_kwh, 1)} kWh`}
        />
      </dl>
    </div>
  );
}

function LineItemRow({ item }: { item: BillLineItem }) {
  // Export credit reduces the bill, so it reads in the export hue -- the same
  // colour that series wears in the chart and the stat cards.
  const isCredit = item.line_type === "export_credit";

  return (
    <tr className="border-b border-hairline last:border-0">
      <td className="px-4 py-2 text-ink-2">
        <span className="flex items-center gap-2">
          {isCredit && (
            <span
              aria-hidden
              className="h-0.5 w-3 rounded-full"
              style={{ backgroundColor: SERIES.export.hex }}
            />
          )}
          {LINE_LABEL[item.line_type] ?? item.line_type}
        </span>
      </td>
      <td className="px-3 py-2 text-ink-muted">
        {item.period_name ? TOU_LABEL[item.period_name] ?? item.period_name : "—"}
      </td>
      <td className="tabular px-3 py-2 text-right text-ink-2">
        {item.quantity_kwh ? `${formatKwh(item.quantity_kwh, 4)} kWh` : "—"}
      </td>
      <td className="tabular px-3 py-2 text-right text-ink-muted">
        {/* The rate frozen onto the line when the bill was cut, not today's. */}
        {item.rate_applied ? formatKwh(item.rate_applied, 4) : "—"}
      </td>
      <td className="tabular px-4 py-2 text-right font-medium text-ink">
        {formatKwh(item.amount, 2)}
      </td>
    </tr>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="tabular mt-0.5 font-medium text-ink">{value}</dd>
    </div>
  );
}
