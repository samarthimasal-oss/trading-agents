import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { LoginScreen } from "./auth/LoginScreen";
import { AppShell } from "./components/AppShell";
import { ToastProvider } from "./components/Toast";
import { LiveProvider } from "./hooks/useLiveSocket";
import { ComingSoonPage } from "./pages/ComingSoon";
import { OverviewPage } from "./pages/Overview";
import { RiskCentrePage } from "./pages/RiskCentre";
import { SettingsPage } from "./pages/Settings";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

function Splash() {
  return (
    <main className="flex min-h-full items-center justify-center bg-ink-950">
      <div className="flex flex-col items-center gap-3" role="status" aria-label="Checking session">
        <div className="h-8 w-8 animate-pulseSoft rounded-full border border-gold-500/50 bg-gold-500/10" />
        <p className="font-display text-[10px] uppercase tracking-[0.24em] text-mist-500">Checking session</p>
      </div>
    </main>
  );
}

function AuthedApp({ token }: { token: string | null }) {
  return (
    <LiveProvider token={token}>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="risk" element={<RiskCentrePage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="live" element={<ComingSoonPage path="/live" />} />
          <Route path="committee" element={<ComingSoonPage path="/committee" />} />
          <Route path="strategies" element={<ComingSoonPage path="/strategies" />} />
          <Route path="market" element={<ComingSoonPage path="/market" />} />
          <Route path="backtests" element={<ComingSoonPage path="/backtests" />} />
          <Route path="learning" element={<ComingSoonPage path="/learning" />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </LiveProvider>
  );
}

function AuthGate() {
  const { status, token } = useAuth();
  if (status === "checking") return <Splash />;
  if (status === "anonymous") return <LoginScreen />;
  return <AuthedApp token={token} />;
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <AuthGate />
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
