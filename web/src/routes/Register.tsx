import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";

import { api, type Role, type TokenResponse } from "../lib/api";
import { HOME_FOR_ROLE, useAuth } from "../auth/AuthContext";
import { AuthShell, FIELD, SubmitButton } from "../components/AuthShell";

/**
 * Registration, by role.
 *
 * Every role has to prove a claim on data that already exists -- a meter
 * serial, an employee code, or a shared staff code. That is what makes a fresh
 * account land on a populated dashboard instead of an empty one, and it is why
 * there is no "just sign up" path.
 */
type Tab = "customer" | "worker" | "government" | "supplier";

const TABS: { id: Tab; label: string; accent: string }[] = [
  { id: "customer", label: "Customer", accent: "bg-portal-customer" },
  { id: "worker", label: "Worker", accent: "bg-portal-worker" },
  { id: "government", label: "Government", accent: "bg-portal-government" },
  { id: "supplier", label: "Supplier", accent: "bg-portal-supplier" },
];

const BLURB: Record<Tab, string> = {
  customer:
    "Enter the serial printed on your billing meter to link an existing service point, or leave it blank to set one up from scratch.",
  worker:
    "Enter your employee code. Your existing work orders, assignments and skills stay attached to your profile.",
  government:
    "Regulator access. Requires the registration code issued to your office.",
  supplier:
    "Utility access. Requires the registration code issued to your organisation.",
};

const CLAIM_FIELD: Record<Tab, { label: string; hint: string; placeholder: string }> = {
  customer: {
    label: "Meter serial (optional)",
    hint: "On the billing meter's faceplate. Demo: SEED-MTR-03 … SEED-MTR-08. Leave blank to build a new connection instead.",
    placeholder: "SEED-MTR-03",
  },
  worker: {
    label: "Employee code",
    hint: "Issued when you joined. Demo: SEED-EMP-002",
    placeholder: "SEED-EMP-002",
  },
  government: {
    label: "Registration code",
    hint: "Demo: GOV-2026",
    placeholder: "GOV-2026",
  },
  supplier: {
    label: "Registration code",
    hint: "Demo: SUP-2026",
    placeholder: "SUP-2026",
  },
};

export default function Register() {
  const { account, isLoading, adopt } = useAuth();
  const navigate = useNavigate();

  const [tab, setTab] = useState<Tab>("customer");
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [phone, setPhone] = useState("");
  const [claim, setClaim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!isLoading && account) {
    return <Navigate to={HOME_FOR_ROLE[account.role]} replace />;
  }

  function switchTab(next: Tab) {
    setTab(next);
    // The claim field means something different per role, so carrying a meter
    // serial into the employee-code field would only produce a confusing 404.
    setClaim("");
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
      };
      let token: TokenResponse;
      if (tab === "customer") {
        token = await api.registerCustomer({
          ...base,
          phone: phone.trim() || null,
          meter_serial: claim.trim(),
        });
      } else if (tab === "worker") {
        token = await api.registerWorker({ ...base, employee_code: claim.trim() });
      } else {
        token = await api.registerStaff(tab, {
          ...base,
          registration_code: claim.trim(),
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

  const field = CLAIM_FIELD[tab];
  // The claim proves ownership of existing data for every role except the
  // customer, who may instead be building a service point from nothing.
  const canSubmit =
    fullName.trim() &&
    email.trim() &&
    password.length >= 8 &&
    (tab === "customer" || claim.trim());

  return (
    <AuthShell
      title="Create an account"
      subtitle="Pick the role you are registering for — each one claims different data."
      wide
      footer={
        <>
          Already registered?{" "}
          <Link to="/login" className="font-medium text-series-import hover:underline">
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
              "flex-1 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              tab === t.id ? `${t.accent} text-white` : "text-ink-2 hover:bg-hairline/60",
            ].join(" ")}
          >
            {t.label}
          </button>
        ))}
      </div>

      <p className="mt-4 text-sm text-ink-2">{BLURB[tab]}</p>

      <form onSubmit={submit} className="mt-6 space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="full_name" className="text-xs font-medium text-ink-2">
              Full name
            </label>
            <input
              id="full_name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className={`mt-1 ${FIELD}`}
              required
            />
          </div>
          <div>
            <label htmlFor="reg-email" className="text-xs font-medium text-ink-2">
              Email
            </label>
            <input
              id="reg-email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className={`mt-1 ${FIELD}`}
              required
            />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label htmlFor="reg-password" className="text-xs font-medium text-ink-2">
              Password
            </label>
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
            <p className="mt-1 text-xs text-ink-muted">At least 8 characters.</p>
          </div>

          {tab === "customer" && (
            <div>
              <label htmlFor="reg-phone" className="text-xs font-medium text-ink-2">
                Phone <span className="text-ink-muted">(optional)</span>
              </label>
              <input
                id="reg-phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                className={`mt-1 ${FIELD}`}
              />
            </div>
          )}
        </div>

        <div>
          <label htmlFor="reg-claim" className="text-xs font-medium text-ink-2">
            {field.label}
          </label>
          <input
            id="reg-claim"
            value={claim}
            onChange={(e) => setClaim(e.target.value)}
            placeholder={field.placeholder}
            className={`mt-1 font-mono ${FIELD}`}
            required={tab !== "customer"}
          />
          <p className="mt-1 text-xs text-ink-muted">{field.hint}</p>
        </div>

        {error && (
          <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
            {error}
          </p>
        )}

        <SubmitButton busy={busy} busyLabel="Creating account…" disabled={!canSubmit}>
          Create account
        </SubmitButton>
      </form>
    </AuthShell>
  );
}
