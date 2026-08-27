import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";

import { HOME_FOR_ROLE, useAuth } from "../auth/AuthContext";
import { AuthShell, FIELD, SubmitButton } from "../components/AuthShell";

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
    </AuthShell>
  );
}
