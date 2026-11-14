import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, getToken, setToken } from "../lib/api";

type AuthStatus = "checking" | "authenticated" | "anonymous";

interface AuthContextValue {
  status: AuthStatus;
  token: string | null;
  login: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setLocalToken] = useState<string | null>(() => getToken());
  const [status, setStatus] = useState<AuthStatus>(() => (getToken() ? "checking" : "anonymous"));

  useEffect(() => {
    if (!token) {
      setStatus("anonymous");
      return;
    }
    let cancelled = false;
    api
      .me()
      .then(() => {
        if (!cancelled) setStatus("authenticated");
      })
      .catch((error: { status?: number }) => {
        if (cancelled) return;
        if (error.status === 401) {
          setToken(null);
          setLocalToken(null);
          setStatus("anonymous");
        } else {
          // The API is unreachable but the token is still present. Let the
          // shell load and show its own connection error rather than looping
          // back to the login screen.
          setStatus("authenticated");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  useEffect(
    () =>
      api.onUnauthorized(() => {
        setLocalToken(null);
        setStatus("anonymous");
      }),
    [],
  );

  const login = useCallback(async (value: string) => {
    await api.login(value);
    setToken(value);
    setLocalToken(value);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setLocalToken(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, token, login, logout }),
    [status, token, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
