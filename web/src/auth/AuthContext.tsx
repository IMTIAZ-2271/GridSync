import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";

import { resetSelectedSite } from "../components/SitePicker";
import {
  api,
  getToken,
  setToken,
  setUnauthorizedHandler,
  type Account,
  type Role,
  type TokenResponse,
} from "../lib/api";

interface AuthState {
  account: Account | null;
  /** True until the stored token has been checked against the server. */
  isLoading: boolean;
  signIn: (email: string, password: string) => Promise<Account>;
  /** Adopt the session a registration call just returned. */
  adopt: (token: TokenResponse) => Account;
  signOut: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

/** Where each role lands after signing in. */
export const HOME_FOR_ROLE: Record<Role, string> = {
  consumer: "/consumer",
  worker: "/worker",
  government: "/government",
  supplier: "/supplier",
  admin: "/supplier",
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [isLoading, setIsLoading] = useState(!!getToken());
  const queryClient = useQueryClient();

  const clearSession = useCallback(() => {
    setToken(null);
    setAccount(null);
    resetSelectedSite();
    // Drop every cached response. Without this the next person to sign in on
    // this browser briefly sees the previous account's sites and bills before
    // their own queries resolve.
    queryClient.clear();
  }, [queryClient]);

  const signOut = useCallback(() => {
    // Fire-and-forget: revoking the token server-side must not block the UI
    // on a slow or sleeping API (Render's free tier sleeps after 15 minutes
    // idle -- a cold start here would leave the button spinning for ~50s).
    // Any failure is swallowed because the local session is torn down either
    // way; an unreachable API just means this token outlives the tab, not
    // that "logout" visibly failed for the person who clicked it.
    api.logout().catch(() => {});
    clearSession();
  }, [clearSession]);

  // A 401 from any request means the token is no longer good -- expired,
  // revoked, or the account is gone. Tear down the session so the route guard
  // redirects, rather than leaving a signed-in shell over failing requests.
  // Local-only: the token that triggered this is already invalid server-side,
  // so calling logout again would be redundant.
  useEffect(() => {
    setUnauthorizedHandler(clearSession);
    return () => setUnauthorizedHandler(null);
  }, [clearSession]);

  // A token in localStorage is a claim, not proof: it may be expired or
  // belong to a deleted account. Verify it against /me before rendering the
  // app as signed in.
  useEffect(() => {
    if (!getToken()) {
      setIsLoading(false);
      return;
    }
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setAccount(me);
      })
      .catch(() => {
        if (!cancelled) setToken(null);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const adopt = useCallback(
    (token: TokenResponse) => {
      setToken(token.access_token);
      setAccount(token.account);
      // The cache may still hold the previous account's rows.
      queryClient.clear();
      return token.account;
    },
    [queryClient],
  );

  const signIn = useCallback(
    async (email: string, password: string) => {
      const token = await api.login({ email, password });
      return adopt(token);
    },
    [adopt],
  );

  const value = useMemo(
    () => ({ account, isLoading, signIn, adopt, signOut }),
    [account, isLoading, signIn, adopt, signOut],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

export const ROLE_LABEL: Record<Role, string> = {
  consumer: "Consumer",
  worker: "Field worker",
  government: "Regulator",
  supplier: "Utility",
  admin: "Admin",
};
