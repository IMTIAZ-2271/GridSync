import { useEffect, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import {
  api,
  type DistributionCompany,
  type Role,
  type TokenResponse,
  type WorkerKind,
} from "../lib/api";
import { HOME_FOR_ROLE, useAuth } from "../auth/AuthContext";
import { AuthShell, FIELD, LABEL, SubmitButton } from "../components/AuthShell";

/**
 * Registration, by role.
 *
 * The four shapes differ because each role's identity hangs off a different
 * key, and the form asks for exactly that key and nothing more:
 *
 * - **Consumer** — National ID only. A billing meter ID is deliberately not
 *   collected here; a household links an existing connection after signing in,
 *   or builds a new one in the onboarding wizard.
 * - **Worker** — National ID, region, and whether they are a government or
 *   private worker. Choosing government reveals the distribution company
 *   picker and turns the submit into an application: an official in that
 *   region has to approve it.
 * - **Government** — the unique code issued to that official, which carries
 *   the district they govern.
 * - **Supplier** — the shared staff code plus the installer they work for.
 */
type Tab = "consumer" | "worker" | "government" | "supplier";

const TABS: { id: Tab; label: string; accent: string }[] = [
  { id: "consumer", label: "Consumer", accent: "bg-portal-consumer" },
  { id: "worker", label: "Worker", accent: "bg-portal-worker" },
  { id: "government", label: "Government", accent: "bg-portal-government" },
  { id: "supplier", label: "Supplier", accent: "bg-portal-supplier" },
];

const BLURB: Record<Tab, string> = {
  consumer:
    "Create your household account. You will add your billing meters once you are signed in — you can have more than one.",
  worker:
    "Register as a field worker. Government workers are approved by an official in their region before they receive work orders.",
  government:
    "Regulator access. Requires the unique official ID issued to you, which sets the district you oversee.",
  supplier:
    "Solar installer access. Requires your organisation's registration code.",
};

/**
 * Reference data the worker tab needs, fetched when that tab opens.
 *
 * Both endpoints are unauthenticated for exactly this reason -- see the note
 * above the handlers in services/api/orgs.py. They are not wrapped in
 * TanStack Query because this page renders outside the authenticated shell
 * and neither list is worth a cache entry that only ever lives for as long as
 * a registration form is open.
 */
function useDistricts(enabled: boolean) {
  const [districts, setDistricts] = useState<string[]>([]);
  useEffect(() => {
    if (!enabled || districts.length) return;
    api
      .listDistricts()
      .then((rows) => setDistricts(rows.map((r) => r.name)))
      .catch(() => setDistricts([]));
  }, [enabled, districts.length]);
  return districts;
}

function useDistributionCompanies(enabled: boolean) {
  const [rows, setRows] = useState<DistributionCompany[]>([]);
  useEffect(() => {
    if (!enabled || rows.length) return;
    api.listDistributionCompanies().then(setRows).catch(() => setRows([]));
  }, [enabled, rows.length]);
  return rows;
}

export default function Register() {
  const { account, isLoading, adopt } = useAuth();
  const navigate = useNavigate();

  const [tab, setTab] = useState<Tab>("consumer");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [phone, setPhone] = useState("");
  const [nationalId, setNationalId] = useState("");

  // Worker
  const [workerKind, setWorkerKind] = useState<WorkerKind>("private");
  const [district, setDistrict] = useState("");
  const [companyId, setCompanyId] = useState("");

  // Government / supplier
  const [code, setCode] = useState("");
  const [supplierCode, setSupplierCode] = useState("");

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const districts = useDistricts(tab === "worker");
  const companies = useDistributionCompanies(
    tab === "worker" && workerKind === "government",
  );

  // Only the utilities that actually serve the chosen region. The server
  // refuses the mismatch with a 422 either way; narrowing the list here means
  // nobody has to discover that by submitting.
  const eligible = district
    ? companies.filter((c) => c.districts.includes(district))
    : companies;

  if (!isLoading && account) {
    return <Navigate to={HOME_FOR_ROLE[account.role]} replace />;
  }

  function switchTab(next: Tab) {
    setTab(next);
    setCode("");
    setSupplierCode("");
    setCompanyId("");
    setError(null);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const base = {
        email: email.trim(),
        password,
        full_name: fullName.trim(),
        national_id: nationalId.trim(),
      };
      let token: TokenResponse;
      if (tab === "consumer") {
        token = await api.registerConsumer({
          ...base,
          phone: phone.trim() || null,
        });
      } else if (tab === "worker") {
        token = await api.registerWorker({
          ...base,
          phone: phone.trim() || null,
          worker_kind: workerKind,
          service_district: district,
          // Sent as null rather than omitted for a private worker: the server
          // refuses a company on a private registration, and being explicit
          // keeps a stale selection from leaking across a kind switch.
          distribution_company_id:
            workerKind === "government" ? companyId : null,
        });
      } else if (tab === "government") {
        token = await api.registerGovernment({
          ...base,
          official_code: code.trim(),
        });
      } else {
        token = await api.registerSupplier({
          ...base,
          registration_code: code.trim(),
          supplier_code: supplierCode.trim(),
        });
      }
      const me = adopt(token);
      navigate(HOME_FOR_ROLE[me.role as Role], { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not register");
    } finally {
      setBusy(false);
    }
  }

  const canSubmit =
    fullName.trim() &&
    email.trim() &&
    password.length >= 8 &&
    nationalId.trim() &&
    (tab !== "worker" ||
      (district && (workerKind === "private" || companyId))) &&
    (tab !== "government" || code.trim()) &&
    (tab !== "supplier" || (code.trim() && supplierCode.trim()));

  return (
    <AuthShell
      title="Create an account"
      subtitle="Pick the role you are registering for — each one asks for something different."
      wide
      footer={
        <>
          Already registered?{" "}
          <Link to="/login" className="font-medium text-series-import underline-offset-4 transition-colors hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      <div
        role="tablist"
        aria-label="Role"
        className="flex flex-wrap gap-1 rounded-lg bg-plane p-1"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => switchTab(t.id)}
            className={[
              "flex-1 rounded-md px-3 py-2 text-[14px] font-medium transition-colors",
              tab === t.id ? `${t.accent} text-white` : "text-ink-2 hover:bg-hairline/60",
            ].join(" ")}
          >
            {t.label}
          </button>
        ))}
      </div>

      <p className="mt-4 text-[14px] text-ink-2">{BLURB[tab]}</p>

      <form onSubmit={submit} className="mt-6 space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="full_name" label="Full name">
            <input
              id="full_name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className={`mt-1 ${FIELD}`}
              required
            />
          </Field>
          <Field id="reg-email" label="Email">
            <input
              id="reg-email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={`mt-1 ${FIELD}`}
              required
            />
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field id="reg-password" label="Password" hint="At least 8 characters.">
            <input
              id="reg-password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={`mt-1 ${FIELD}`}
              minLength={8}
              required
            />
          </Field>
          <Field
            id="reg-nid"
            label="National ID"
            hint="10, 13 or 17 digits. Spaces are fine."
          >
            <input
              id="reg-nid"
              inputMode="numeric"
              value={nationalId}
              onChange={(e) => setNationalId(e.target.value)}
              placeholder="1990 123456"
              className={`mt-1 font-mono ${FIELD}`}
              required
            />
          </Field>
        </div>

        {(tab === "consumer" || tab === "worker") && (
          <Field id="reg-phone" label="Phone" optional>
            <input
              id="reg-phone"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className={`mt-1 ${FIELD}`}
            />
          </Field>
        )}

        {tab === "worker" && (
          <>
            <fieldset>
              <legend className={LABEL}>
                Which kind of worker are you?
              </legend>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                <KindOption
                  value="private"
                  current={workerKind}
                  onSelect={setWorkerKind}
                  title="Private"
                  detail="Independent work in your region. You will not receive government work orders."
                />
                <KindOption
                  value="government"
                  current={workerKind}
                  onSelect={setWorkerKind}
                  title="Government"
                  detail="Employed by a distribution company. Approved by an official in your region."
                />
              </div>
            </fieldset>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                id="reg-district"
                label="Region"
                hint="You will only receive work in this region."
              >
                <select
                  id="reg-district"
                  value={district}
                  onChange={(e) => {
                    setDistrict(e.target.value);
                    // A company that does not serve the new region must not
                    // stay selected -- the server would refuse it on submit.
                    setCompanyId("");
                  }}
                  className={`mt-1 ${FIELD}`}
                  required
                >
                  <option value="">Select a region…</option>
                  {districts.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </Field>

              {workerKind === "government" && (
                <Field
                  id="reg-company"
                  label="Distribution company"
                  hint={
                    district
                      ? `Utilities serving ${district}.`
                      : "Pick a region first."
                  }
                >
                  <select
                    id="reg-company"
                    value={companyId}
                    onChange={(e) => setCompanyId(e.target.value)}
                    className={`mt-1 ${FIELD}`}
                    disabled={!district}
                    required
                  >
                    <option value="">Select a company…</option>
                    {eligible.map((c) => (
                      <option key={c.company_id} value={c.company_id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </Field>
              )}
            </div>

            {workerKind === "government" && (
              <p className="rounded-md bg-plane px-3 py-2 text-[14px] leading-relaxed text-ink-2">
                Your registration is sent to the government officials for
                {district ? ` ${district}` : " your region"}. You can sign in
                straight away, but work orders only start arriving once it is
                approved.
              </p>
            )}
          </>
        )}

        {tab === "government" && (
          <Field
            id="reg-code"
            label="Official ID"
            hint="Issued to you personally, and usable once."
          >
            <input
              id="reg-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              className={`mt-1 font-mono ${FIELD}`}
              required
            />
          </Field>
        )}

        {tab === "supplier" && (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              id="reg-code"
              label="Registration code"
              hint="Given to you by your organisation."
            >
              <input
                id="reg-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className={`mt-1 font-mono ${FIELD}`}
                required
              />
            </Field>
            <Field
              id="reg-supplier"
              label="Your organisation"
              hint="The installer you work for."
            >
              <input
                id="reg-supplier"
                value={supplierCode}
                onChange={(e) => setSupplierCode(e.target.value)}
                placeholder="NOOR"
                className={`mt-1 font-mono ${FIELD}`}
                required
              />
            </Field>
          </div>
        )}

        {error && (
          <p className="rounded-lg border border-status-critical/20 bg-status-critical/8 px-3.5 py-2.5 text-[14px] leading-relaxed text-status-critical">
            {error}
          </p>
        )}

        <SubmitButton busy={busy} busyLabel="Creating account…" disabled={!canSubmit}>
          {tab === "worker" && workerKind === "government"
            ? "Submit application"
            : "Create account"}
        </SubmitButton>
      </form>
    </AuthShell>
  );
}

function Field({
  id,
  label,
  hint,
  optional,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  optional?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label htmlFor={id} className={LABEL}>
        {label}
        {optional && <span className="text-ink-muted"> (optional)</span>}
      </label>
      {children}
      {hint && <p className="mt-1.5 text-[14px] text-ink-2">{hint}</p>}
    </div>
  );
}

function KindOption({
  value,
  current,
  onSelect,
  title,
  detail,
}: {
  value: WorkerKind;
  current: WorkerKind;
  onSelect: (v: WorkerKind) => void;
  title: string;
  detail: string;
}) {
  const selected = current === value;
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={() => onSelect(value)}
      className={[
        "rounded-lg border px-3 py-2 text-left transition-colors",
        selected
          ? "border-portal-worker bg-portal-worker/10"
          : "border-hairline hover:bg-hairline/40",
      ].join(" ")}
    >
      <span className="block text-[14px] font-medium text-ink-1">{title}</span>
      <span className="mt-1 block text-[14px] text-ink-2">{detail}</span>
    </button>
  );
}
