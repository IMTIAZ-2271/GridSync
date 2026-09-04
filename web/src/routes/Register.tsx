import { useEffect, useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import {
  api,
  type DistributionCompany,
  type Role,
  type SupplierCompanyBrief,
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
 *   private worker. Choosing government also asks which utility employs them.
 *   Either way it is an application: an official in that region has to approve
 *   it before any work reaches them.
 * - **Government** — the unique code issued to that official, which carries
 *   the district they govern. The only code left on this page.
 * - **Supplier** — the installer they work for and the region they work it in.
 *   Also an application. There used to be a shared staff code here; it was one
 *   string for every firm in the city, so it proved nothing, and it is gone.
 *   The official checks the name, National ID and organisation instead.
 *
 * Two of the four therefore end on a waiting screen rather than in a portal
 * (see auth/RequireAuth.tsx), which is why the submit button says what it is
 * about to do.
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
    "Register as a field worker. An official in your region approves your registration before any work reaches you.",
  government:
    "Regulator access. Requires the unique official ID issued to you, which sets the district you oversee.",
  supplier:
    "Solar installer staff. An official in your region checks your details against their records before your account is activated.",
};

/**
 * Reference data the two staff tabs need, fetched when one of them opens.
 *
 * All three endpoints are unauthenticated for exactly this reason -- see the
 * note above the handlers in services/api/orgs.py. They are not wrapped in
 * TanStack Query because this page renders outside the authenticated shell
 * and none of the lists is worth a cache entry that only ever lives for as
 * long as a registration form is open.
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

/**
 * The installers, for the supplier tab.
 *
 * `/api/supplier-companies` is the narrow, unauthenticated list — name, code
 * and coverage, no ratings or contact details. Picking a firm here is a claim,
 * not a credential: an official verifies it.
 */
function useSupplierCompanies(enabled: boolean) {
  const [rows, setRows] = useState<SupplierCompanyBrief[]>([]);
  useEffect(() => {
    if (!enabled || rows.length) return;
    api.listSupplierCompanies().then(setRows).catch(() => setRows([]));
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

  // Government
  const [code, setCode] = useState("");
  // Supplier
  const [supplierCode, setSupplierCode] = useState("");

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Both staff tabs pick a region: a worker's queue is scoped to it, and a
  // supplier's application is decided by its official.
  const districts = useDistricts(tab === "worker" || tab === "supplier");
  const companies = useDistributionCompanies(
    tab === "worker" && workerKind === "government",
  );
  const installers = useSupplierCompanies(tab === "supplier");

  // Only the utilities that actually serve the chosen region. The server
  // refuses the mismatch with a 422 either way; narrowing the list here means
  // nobody has to discover that by submitting.
  const eligible = district
    ? companies.filter((c) => c.districts.includes(district))
    : companies;

  // Same rule for installers, read the other way round: the firm is chosen
  // first (it is the thing the applicant actually knows), so the region list
  // narrows to where that firm works.
  const installerDistricts = supplierCode
    ? (installers.find((f) => f.code === supplierCode)?.districts ?? [])
    : null;
  const supplierRegions = installerDistricts
    ? districts.filter((d) => installerDistricts.includes(d))
    : districts;

  if (!isLoading && account) {
    return <Navigate to={HOME_FOR_ROLE[account.role]} replace />;
  }

  function switchTab(next: Tab) {
    setTab(next);
    setCode("");
    setSupplierCode("");
    setCompanyId("");
    // The region means different things on the two tabs it appears on, and a
    // value carried across would be a choice nobody made.
    setDistrict("");
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
          phone: phone.trim() || null,
          supplier_code: supplierCode.trim(),
          service_district: district,
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
    (tab !== "supplier" || (supplierCode.trim() && district));

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

        {tab !== "government" && (
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

            <p className="rounded-md bg-plane px-3 py-2 text-[14px] leading-relaxed text-ink-2">
              Your registration is sent to the government officials for
              {district ? ` ${district}` : " your region"}. You can sign in
              straight away and check where it stands; work orders start
              arriving once it is approved.
            </p>
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
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field
                id="reg-supplier"
                label="Your organisation"
                hint="The installer you work for."
              >
                <select
                  id="reg-supplier"
                  value={supplierCode}
                  onChange={(e) => {
                    setSupplierCode(e.target.value);
                    // A region the new firm does not cover must not stay
                    // selected -- the server would refuse it on submit.
                    setDistrict("");
                  }}
                  className={`mt-1 ${FIELD}`}
                  required
                >
                  <option value="">Select an installer…</option>
                  {installers.map((f) => (
                    <option key={f.supplier_id} value={f.code}>
                      {f.name}
                    </option>
                  ))}
                </select>
              </Field>

              <Field
                id="reg-supplier-district"
                label="Region"
                hint={
                  supplierCode
                    ? "Where you work for them. An official here approves you."
                    : "Pick an installer first."
                }
              >
                <select
                  id="reg-supplier-district"
                  value={district}
                  onChange={(e) => setDistrict(e.target.value)}
                  className={`mt-1 ${FIELD}`}
                  disabled={!supplierCode}
                  required
                >
                  <option value="">Select a region…</option>
                  {supplierRegions.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <p className="rounded-md bg-plane px-3 py-2 text-[14px] leading-relaxed text-ink-2">
              Your registration is sent to the government officials for
              {district ? ` ${district}` : " that region"}, who check your
              name, National ID and organisation against their records. You can
              sign in straight away and see where it stands.
            </p>
          </>
        )}

        {error && (
          <p className="rounded-lg border border-status-critical/20 bg-status-critical/8 px-3.5 py-2.5 text-[14px] leading-relaxed text-status-critical">
            {error}
          </p>
        )}

        <SubmitButton busy={busy} busyLabel="Creating account…" disabled={!canSubmit}>
          {/* Says what actually happens. Two of the four tabs end on a waiting
              screen, and a button reading "Create account" would be promising
              a portal that is not going to open yet. */}
          {tab === "worker" || tab === "supplier"
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
