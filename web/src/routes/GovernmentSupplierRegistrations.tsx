import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiError,
  api,
  queryKeys,
  type PendingSupplier,
  type SupplierCompany,
} from "../lib/api";
import {
  VIEWS,
  isUnread,
  unreadRowClass,
  useMarkViewSeen,
} from "../lib/unread";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from "../components/ui";

/**
 * Approving an installer's staff account — and resolving the organisation.
 *
 * This queue replaced a shared registration code: one string, the same for
 * every firm in the city, never rotated. Nothing on the registration form is
 * treated as evidence any more. The applicant types a name, a National ID and
 * an organisation, and an official checks all three against records the form
 * cannot reach.
 *
 * **The organisation is a claim, and approving is what makes it real.** The
 * applicant typed a string; nothing was looked up, matched or created from it.
 * Approving says which `supplier_company` that string means — an existing firm,
 * or a new one created here. That is a judgement rather than a lookup because
 * a firm is one row however many staff it has, and it is what a household
 * picks, applies to and rates: if a typed name could match or create a firm on
 * its own, three spellings would be three firms with three reputations.
 *
 * So the row offers a suggestion and never an answer. `suggested_supplier_id`
 * is an *exact* case-insensitive match — deliberately exact, because the
 * official is the only check there is and a plausible-looking wrong suggestion
 * is the one thing that could get waved through. No suggestion means "nothing
 * obvious matched", not "this is a new firm"; which of the two it is, is
 * exactly what the official is here to decide.
 *
 * Scope is the official's own district, from the server — and it is the
 * district the *applicant registered for*, not everywhere their firm works, so
 * approving here never vouches for a colleague next door.
 *
 * Rejection asks for a reason; approval asks which firm. Same asymmetry as the
 * other queues: approval is the expected outcome, and "rejected" with no reason
 * is not something an applicant can act on.
 */
export default function GovernmentSupplierRegistrations() {
  // Marks this list seen on open and hands back the watermark it replaced, so
  // rows that arrived since the last visit are lit for exactly this render.
  const watermark = useMarkViewSeen(VIEWS.governmentSupplierRegistrations);
  const queryClient = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);

  const pending = useQuery({
    queryKey: queryKeys.pendingSupplierRegistrations(),
    queryFn: () => api.pendingSupplierRegistrations(),
  });

  // The firms an official can link a claim to. Authenticated and already used
  // by other pages, so this is usually a warm cache entry rather than a
  // request. Not filtered by district: a firm that has never worked here is a
  // perfectly ordinary thing for a new applicant to belong to, and approving
  // is what records that it does (`add_supplier_service_area`).
  const suppliers = useQuery({
    queryKey: queryKeys.suppliers(),
    queryFn: () => api.listSuppliers(),
  });

  const decide = useMutation({
    mutationFn: ({
      accountId,
      body,
    }: {
      accountId: string;
      body: Parameters<typeof api.decideSupplierRegistration>[1];
    }) => api.decideSupplierRegistration(accountId, body),
    // Invalidate rather than splice the row out: the server maintains
    // approved_at, rejection_reason and the link, and a decision someone else
    // made in the meantime should surface here rather than be papered over.
    // The supplier list too -- an approval may have just created a firm.
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: queryKeys.pendingSupplierRegistrations(),
        }),
        queryClient.invalidateQueries({ queryKey: queryKeys.suppliers() }),
      ]);
      setOpen(null);
    },
  });

  // A 409 means another official decided it first, or the licence number is
  // already held by an existing firm. Both are worth saying plainly -- the
  // refetch will already have changed the page under them.
  const conflict =
    decide.error instanceof ApiError && decide.error.status === 409;

  return (
    <Card>
      <CardHeader
        title="Supplier approvals"
        subtitle="Installer staff awaiting a decision in your district"
        action={
          pending.data ? (
            <Badge tone={pending.data.length > 0 ? "warning" : "good"}>
              {pending.data.length} pending
            </Badge>
          ) : undefined
        }
      />

      {conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-ink-2">
          {decide.error instanceof ApiError ? decide.error.message : null}
        </p>
      )}
      {decide.error && !conflict && (
        <p className="border-b border-hairline px-5 py-3 text-sm text-status-critical">
          {decide.error.message}
        </p>
      )}

      {pending.isPending ? (
        <div className="space-y-3 p-5">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
        </div>
      ) : pending.error ? (
        <ErrorState error={pending.error} />
      ) : pending.data.length === 0 ? (
        <EmptyState
          title="Nothing waiting"
          hint="Anyone registering to work for a solar installer in your district appears here. Check their name, National ID and the organisation they have named against your records, then say which registered installer that organisation is. Until you do, they cannot open the supplier portal."
        />
      ) : (
        <ul className="divide-y divide-hairline">
          {pending.data.map((row) => (
            <SupplierRow
              key={row.account_id}
              unread={isUnread(row.registered_at, watermark)}
              row={row}
              suppliers={suppliers.data ?? []}
              panel={open === row.account_id ? "open" : null}
              onOpen={() => setOpen(row.account_id)}
              onClose={() => setOpen(null)}
              busy={decide.isPending}
              onDecide={(body) =>
                decide.mutate({ accountId: row.account_id, body })
              }
            />
          ))}
        </ul>
      )}
    </Card>
  );
}

type DecisionBody = Parameters<typeof api.decideSupplierRegistration>[1];

function SupplierRow({
  unread,
  row,
  suppliers,
  panel,
  onOpen,
  onClose,
  busy,
  onDecide,
}: {
  /** Arrived since this account last opened the list. */
  unread: boolean;
  row: PendingSupplier;
  suppliers: SupplierCompany[];
  panel: "open" | null;
  onOpen: () => void;
  onClose: () => void;
  busy: boolean;
  onDecide: (body: DecisionBody) => void;
}) {
  const waiting = Math.floor(
    (Date.now() - new Date(row.registered_at).getTime()) / 86_400_000,
  );

  return (
    <li className={`px-5 py-4 ${unreadRowClass(unread)}`}>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="font-medium text-ink">{row.full_name}</p>
          <p className="mt-0.5 text-sm text-ink-muted">
            {row.email}
            {row.phone ? ` · ${row.phone}` : ""}
          </p>
          {/* The two facts the decision is made on, given their own line
              rather than folded into the meta row. */}
          <dl className="mt-2 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <Fact label="National ID" value={row.national_id ?? "—"} mono />
            <Fact label="Role" value={row.job_title ?? "Not stated"} />
          </dl>
          {/* Quoted, because it is what somebody typed rather than something
              the system knows. */}
          <p className="mt-2 text-sm text-ink-2">
            Claims to work for{" "}
            <span className="font-medium text-ink">
              “{row.claimed_organisation}”
            </span>
          </p>
          <p className="mt-1 text-xs text-ink-muted">
            {row.suggested_supplier_name
              ? `Matches a registered installer: ${row.suggested_supplier_name}`
              : "No registered installer by that exact name"}{" "}
            · {row.service_district} · registered{" "}
            {waiting === 0
              ? "today"
              : `${waiting} day${waiting === 1 ? "" : "s"} ago`}
          </p>
        </div>

        {!panel && (
          <button
            type="button"
            disabled={busy}
            onClick={onOpen}
            className="shrink-0 rounded-lg bg-ink px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50"
          >
            Review
          </button>
        )}
      </div>

      {panel && (
        <DecisionPanel
          row={row}
          suppliers={suppliers}
          busy={busy}
          onCancel={onClose}
          onDecide={onDecide}
        />
      )}
    </li>
  );
}

/**
 * The decision itself: link the claim to a firm and approve, or reject.
 *
 * Approving cannot be a bare button here, because an approval that did not say
 * which firm would leave someone a supplier of nothing — the server refuses it
 * (`supplier_approved_has_firm`), and offering a control that always fails
 * would be worse than not offering it.
 */
function DecisionPanel({
  row,
  suppliers,
  busy,
  onCancel,
  onDecide,
}: {
  row: PendingSupplier;
  suppliers: SupplierCompany[];
  busy: boolean;
  onCancel: () => void;
  onDecide: (body: DecisionBody) => void;
}) {
  // Pre-selected when the typed name matched exactly, which is the ordinary
  // case and makes this one click. A match is still shown rather than applied
  // silently -- the official confirms it, they do not discover it afterwards.
  const [mode, setMode] = useState<"link" | "create" | "reject">(
    row.suggested_supplier_id ? "link" : "create",
  );
  const [supplierId, setSupplierId] = useState(row.suggested_supplier_id ?? "");
  const [newName, setNewName] = useState(row.claimed_organisation);
  const [licence, setLicence] = useState("");
  const [reason, setReason] = useState("");

  const canSubmit =
    mode === "link"
      ? Boolean(supplierId)
      : mode === "create"
        ? newName.trim().length >= 2
        : true;

  function submit() {
    if (mode === "reject") {
      onDecide({ decision: "reject", reason: reason.trim() || null });
    } else if (mode === "link") {
      onDecide({ decision: "approve", supplier_id: supplierId });
    } else {
      onDecide({
        decision: "approve",
        new_supplier: {
          name: newName.trim(),
          license_no: licence.trim() || null,
        },
      });
    }
  }

  return (
    <div className="mt-3 space-y-3 rounded-lg border border-hairline p-3">
      <fieldset>
        <legend className="text-sm font-medium text-ink-2">
          Which organisation is this?
        </legend>
        <div className="mt-2 space-y-2">
          <Choice
            checked={mode === "link"}
            onSelect={() => setMode("link")}
            label="A registered installer"
            detail="Link this person to a firm already on the register."
          />
          {mode === "link" && (
            <select
              value={supplierId}
              onChange={(e) => setSupplierId(e.target.value)}
              className="ml-6 w-[calc(100%-1.5rem)] rounded-lg border border-hairline bg-surface px-3 py-2 text-sm text-ink"
            >
              <option value="">Select an installer…</option>
              {suppliers.map((s) => (
                <option key={s.supplier_id} value={s.supplier_id}>
                  {s.name}
                  {s.license_no ? ` · ${s.license_no}` : ""}
                </option>
              ))}
            </select>
          )}

          <Choice
            checked={mode === "create"}
            onSelect={() => setMode("create")}
            label="An installer not yet on the register"
            detail="Adds the firm. Households in this district will be able to choose it."
          />
          {mode === "create" && (
            <div className="ml-6 grid gap-2 sm:grid-cols-2">
              <label className="block text-sm">
                <span className="text-ink-2">Registered name</span>
                <input
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
              </label>
              <label className="block text-sm">
                <span className="text-ink-2">Licence number (optional)</span>
                <input
                  value={licence}
                  onChange={(e) => setLicence(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
                />
              </label>
            </div>
          )}

          <Choice
            checked={mode === "reject"}
            onSelect={() => setMode("reject")}
            label="Reject this registration"
            detail="Nothing is linked. Tell them what to fix."
          />
          {mode === "reject" && (
            <label className="ml-6 block text-sm">
              <span className="text-ink-2">Why is this being rejected?</span>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="Not listed as staff by this installer"
                className="mt-1 w-full rounded-lg border border-hairline bg-surface px-3 py-2 text-ink"
              />
              <span className="mt-1 block text-xs text-ink-muted">
                This is sent to the applicant.
              </span>
            </label>
          )}
        </div>
      </fieldset>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={busy || !canSubmit}
          onClick={submit}
          className={[
            "rounded-lg px-3.5 py-1.5 text-sm font-medium text-surface disabled:opacity-50",
            mode === "reject" ? "bg-status-critical" : "bg-ink",
          ].join(" ")}
        >
          {busy
            ? "Saving…"
            : mode === "reject"
              ? "Confirm rejection"
              : "Approve"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="text-sm text-ink-muted underline"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function Choice({
  checked,
  onSelect,
  label,
  detail,
}: {
  checked: boolean;
  onSelect: () => void;
  label: string;
  detail: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      onClick={onSelect}
      className="flex w-full items-start gap-2 text-left"
    >
      <span
        aria-hidden="true"
        className={[
          "mt-0.5 h-4 w-4 shrink-0 rounded-full border",
          checked
            ? "border-ink bg-ink shadow-[inset_0_0_0_3px_var(--color-surface)]"
            : "border-hairline",
        ].join(" ")}
      />
      <span className="min-w-0">
        <span className="block text-sm font-medium text-ink">{label}</span>
        <span className="block text-xs text-ink-muted">{detail}</span>
      </span>
    </button>
  );
}

function Fact({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-2">
      <dt className="shrink-0 text-ink-muted">{label}</dt>
      <dd className={`min-w-0 truncate text-ink-2 ${mono ? "tabular" : ""}`}>
        {value}
      </dd>
    </div>
  );
}
