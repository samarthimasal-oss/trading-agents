import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BrainCircuit,
  FlaskConical,
  LayoutDashboard,
  LogOut,
  Menu,
  Radar,
  Settings,
  ShieldAlert,
  SlidersHorizontal,
  TerminalSquare,
  Users,
  X,
} from "lucide-react";

import { useAuth } from "../auth/AuthContext";
import { useLive } from "../hooks/useLiveSocket";
import { Badge, StatusDot } from "../components/Panel";
import { money, utcClock } from "../lib/format";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, ready: true },
  { to: "/live", label: "Live Trading", icon: Activity, ready: false },
  { to: "/committee", label: "AI Committee", icon: Users, ready: false },
  { to: "/strategies", label: "Strategy Desk", icon: SlidersHorizontal, ready: false },
  { to: "/market", label: "Market Intelligence", icon: Radar, ready: false },
  { to: "/backtests", label: "Backtesting", icon: FlaskConical, ready: false },
  { to: "/learning", label: "Learning Centre", icon: BrainCircuit, ready: false },
  { to: "/risk", label: "Risk Centre", icon: ShieldAlert, ready: true },
  { to: "/settings", label: "Settings", icon: Settings, ready: true },
];

const TITLES: Record<string, { title: string; eyebrow: string }> = {
  "/": { title: "Overview", eyebrow: "Supervision" },
  "/live": { title: "Live Trading", eyebrow: "Execution" },
  "/committee": { title: "AI Committee", eyebrow: "Reasoning" },
  "/strategies": { title: "Strategy Desk", eyebrow: "Research" },
  "/market": { title: "Market Intelligence", eyebrow: "Context" },
  "/backtests": { title: "Backtesting", eyebrow: "Validation" },
  "/learning": { title: "Learning Centre", eyebrow: "Post-trade" },
  "/risk": { title: "Risk Centre", eyebrow: "Governor" },
  "/settings": { title: "Settings", eyebrow: "Configuration" },
};

function wsTone(status: string): "ok" | "warn" | "bad" | "idle" {
  if (status === "open") return "ok";
  if (status === "connecting" || status === "reconnecting") return "warn";
  if (status === "unauthorized") return "bad";
  return "idle";
}

function wsLabel(status: string): string {
  if (status === "open") return "Live";
  if (status === "connecting") return "Connecting";
  if (status === "reconnecting") return "Reconnecting";
  if (status === "unauthorized") return "Auth lost";
  return "Offline";
}

export function AppShell() {
  const { logout } = useAuth();
  const { status, snapshot } = useLive();
  const location = useLocation();
  const [open, setOpen] = useState(false);
  const [clock, setClock] = useState(() => utcClock());

  useEffect(() => {
    const timer = window.setInterval(() => setClock(utcClock()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setOpen(false);
    window.scrollTo(0, 0);
  }, [location.pathname]);

  const meta = TITLES[location.pathname] ?? { title: "Console", eyebrow: "TradingAgents" };
  const runnerMode = snapshot?.runner?.mode ?? "unknown";
  const paper = runnerMode !== "LIVE";

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex h-16 items-center justify-between border-b border-ink-700 px-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center border border-gold-500/45 bg-gold-500/10">
            <TerminalSquare className="h-4 w-4 text-gold-300" aria-hidden />
          </div>
          <div>
            <p className="font-display text-xs font-semibold uppercase tracking-[0.2em]">TradingAgents</p>
            <p className="text-[9px] uppercase tracking-[0.18em] text-mist-600">Control room</p>
          </div>
        </div>
        <button
          type="button"
          className="text-mist-500 hover:text-mist-100 lg:hidden"
          onClick={() => setOpen(false)}
          aria-label="Close navigation"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="border-b border-ink-700/70 px-4 py-3">
        <div className="flex items-center justify-between">
          <span className="label">Mode</span>
          <Badge tone={paper ? "gold" : "bad"}>{paper ? "PAPER" : "LIVE"}</Badge>
        </div>
        <p className="mt-2 text-[10px] leading-relaxed text-mist-600">
          {paper
            ? "Dry-run runner. No live order path is exposed."
            : "Live mode reported by the runner. Verify before acting."}
        </p>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 py-3" aria-label="Primary">
        <ul className="space-y-0.5">
          {NAV.map((item) => {
            const Icon = item.icon;
            return (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) =>
                    `group flex items-center gap-3 border-l-2 px-3 py-2.5 transition-colors ${
                      isActive
                        ? "border-gold-400 bg-gold-500/[0.07] text-mist-100"
                        : "border-transparent text-mist-500 hover:bg-ink-800/60 hover:text-mist-200"
                    }`
                  }
                >
                  {({ isActive }) => (
                    <>
                      <Icon
                        className={`h-4 w-4 shrink-0 ${isActive ? "text-gold-300" : "text-mist-600 group-hover:text-mist-400"}`}
                        aria-hidden
                      />
                      <span className="flex-1 font-display text-[11px] font-medium uppercase tracking-[0.11em]">
                        {item.label}
                      </span>
                      {!item.ready ? (
                        <span className="border border-ink-600 px-1.5 py-0.5 text-[8px] uppercase tracking-[0.12em] text-mist-600">
                          next
                        </span>
                      ) : null}
                    </>
                  )}
                </NavLink>
              </li>
            );
          })}
        </ul>
      </nav>

      <div className="border-t border-ink-700 px-4 py-3">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="label">MT5 account</p>
            <p className="mt-1 truncate font-mono text-xs text-mist-300">
              {snapshot?.account?.login ?? "not connected"}
            </p>
            <p className="mt-0.5 font-mono text-[10px] text-mist-600">
              {snapshot?.account?.available === false
                ? "account unavailable"
                : money(snapshot?.account?.equity, snapshot?.account?.currency ?? "USD")}
            </p>
          </div>
          <button
            type="button"
            className="text-mist-500 hover:text-signal-down"
            onClick={logout}
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <StatusDot tone={wsTone(status)} />
          <span className="text-[10px] uppercase tracking-[0.12em] text-mist-600">
            WebSocket {wsLabel(status)}
          </span>
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex min-h-full">
      <aside className="sticky top-0 hidden h-screen w-[232px] shrink-0 border-r border-ink-700 bg-ink-900/75 backdrop-blur lg:block">
        {sidebar}
      </aside>

      {open ? (
        <div className="fixed inset-0 z-30 lg:hidden">
          <div className="absolute inset-0 bg-ink-950/80 backdrop-blur-sm" onClick={() => setOpen(false)} aria-hidden />
          <aside className="relative h-full w-[268px] border-r border-ink-700 bg-ink-900">{sidebar}</aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between gap-3 border-b border-ink-700 bg-ink-950/85 px-4 backdrop-blur-md sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="text-mist-400 hover:text-mist-100 lg:hidden"
              onClick={() => setOpen(true)}
              aria-label="Open navigation"
            >
              <Menu className="h-5 w-5" aria-hidden />
            </button>
            <div className="min-w-0">
              <p className="eyebrow">{meta.eyebrow}</p>
              <h1 className="truncate font-display text-lg font-semibold tracking-tight">{meta.title}</h1>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2 sm:gap-4">
            <div className="hidden items-center gap-2 sm:flex">
              <StatusDot tone={wsTone(status)} />
              <span className="text-[10px] uppercase tracking-[0.14em] text-mist-500">{wsLabel(status)}</span>
            </div>
            <div className="hidden h-6 w-px bg-ink-700 sm:block" />
            <div className="text-right">
              <p className="font-mono text-xs tabular-nums text-mist-200">{clock}</p>
              <p className="text-[9px] uppercase tracking-[0.14em] text-mist-600">UTC</p>
            </div>
            <div className="hidden h-6 w-px bg-ink-700 sm:block" />
            <Badge tone={paper ? "gold" : "bad"}>{paper ? "DRY-RUN" : "LIVE"}</Badge>
          </div>
        </header>

        <main className="mx-auto w-full max-w-[1600px] flex-1 px-4 py-5 sm:px-6 sm:py-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
