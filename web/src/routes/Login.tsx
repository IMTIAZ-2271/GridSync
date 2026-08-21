import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";

import { HOME_FOR_ROLE, useAuth } from "../auth/AuthContext";
import { AuthShell, FIELD, SubmitButton } from "../components/AuthShell";

const DEMO_LOGINS = [
  { email: "customer@demo.com", label: "Customer", hint: "Seed Site 01 — solar, credit balance" },
  { email: "worker@demo.com", label: "Field worker", hint: "6 assigned work orders" },
  { email: "gov@demo.com", label: "Regulator", hint: "3 pending agreements" },
  { email: "supplier@demo.com", label: "Utility", hint: "All 8 sites" },
];
const DEMO_PASSWORD = "demo1234";

export default function Login() {
  const { account, isLoading, signIn } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Already signed in: go where they were headed, or to their portal.
  if (!isLoading && account) {
    const from = (location.state as { from?: Location } | null)?.from;
    return <Navigate to={from?.pathname ?? HOME_FOR_ROLE[account.role]} replace />;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const me = await signIn(email.trim(), password);
      const from = (location.state as { from?: Location } | null)?.from;
      navigate(from?.pathname ?? HOME_FOR_ROLE[me.role], { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign in");
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell
      title="Sign in"
      subtitle="Four portals, one account. Where you land depends on your role."
      footer={
        <>
          No account yet?{" "}
          <Link to="/register" className="font-medium text-series-import hover:underline">
            Register
          </Link>
        </>
      }
    >
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label htmlFor="email" className="text-xs font-medium text-ink-2">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={`mt-1 ${FIELD}`}
            required
          />
        </div>

        <div>
          <label htmlFor="password" className="text-xs font-medium text-ink-2">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={`mt-1 ${FIELD}`}
            required
          />
        </div>

        {error && (
          <p className="rounded-md bg-status-critical/10 px-3 py-2 text-xs text-status-critical">
            {error}
          </p>
        )}

        <SubmitButton busy={busy} busyLabel="Signing in…">
          Sign in
        </SubmitButton>
      </form>

      {/* Demo affordance. Nothing here would ship with real accounts behind it. */}
      <div className="mt-8 border-t border-hairline pt-5">
        <p className="text-xs font-medium text-ink-2">Demo accounts</p>
        <p className="mt-1 text-xs text-ink-muted">
          Password <code className="rounded bg-plane px-1 py-0.5">{DEMO_PASSWORD}</code> for all
          four. Select one to fill the form.
        </p>
        <ul className="mt-3 space-y-1.5">
          {DEMO_LOGINS.map((d) => (
            <li key={d.email}>
              <button
                type="button"
                onClick={() => {
                  setEmail(d.email);
                  setPassword(DEMO_PASSWORD);
                  setError(null);
                }}
                className="flex w-full items-baseline gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-plane"
              >
                <span className="text-xs font-medium text-ink">{d.label}</span>
                <span className="text-xs text-ink-muted">{d.hint}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </AuthShell>
  );
}
