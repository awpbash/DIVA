import { createContext, useContext, useEffect, useState } from "react";
import { Account, apiLogin, apiLogout, apiMe, getSessionToken, setSessionToken } from "./api";
import { tabsForRole } from "./rbac";

// Auth context — the logged-in account drives RBAC across the whole app (role comes from
// the account, not a toggle). Passwordless: a token from /auth/login is kept in localStorage
// and restored on reload via /auth/me. The server also returns the account's CAPABILITIES
// (which tabs it may use) so the UI renders exactly what the API will allow.

export type Role = "admin" | "confidential" | "default";

interface AuthState {
  user: Account | null;
  /** Tabs this account may use — server-declared, static fallback until known. */
  tabs: string[];
  loading: boolean;
  login: (email: string) => Promise<void>;
  logout: () => void;
}

const AuthCtx = createContext<AuthState>({
  user: null, tabs: tabsForRole(null), loading: true,
  login: async () => {}, logout: () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<Account | null>(null);
  const [tabs, setTabs] = useState<string[]>(tabsForRole(null));
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Restore a prior session on reload — only when a token exists; a first
    // visit has nothing to restore (calling /auth/me anyway just logs a 401).
    if (!getSessionToken()) { setLoading(false); return; }
    apiMe()
      .then(res => {
        setUser(res?.user ?? null);
        setTabs(res?.capabilities?.tabs ?? tabsForRole(res?.user?.role));
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    // A 401 anywhere (session revoked / account deleted) drops back to login.
    const onExpired = () => { setUser(null); setTabs(tabsForRole(null)); };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

  const login = async (email: string) => {
    const { token, user, capabilities } = await apiLogin(email);
    setSessionToken(token);
    setUser(user);
    setTabs(capabilities?.tabs ?? tabsForRole(user.role));
  };

  const logout = () => {
    apiLogout();
    setSessionToken(null);
    setUser(null);
    setTabs(tabsForRole(null));
  };

  return (
    <AuthCtx.Provider value={{ user, tabs, loading, login, logout }}>
      {children}
    </AuthCtx.Provider>
  );
}

export const useAuth = () => useContext(AuthCtx);

/** The logged-in user's role, defaulting to the most-restricted level. */
export const useRole = (): Role => {
  const { user } = useAuth();
  const r = (user?.role || "default") as Role;
  return (["admin", "confidential", "default"] as const).includes(r) ? r : "default";
};
