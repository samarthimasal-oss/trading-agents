import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { KeyRound, ShieldCheck, TerminalSquare } from "lucide-react";

import { useAuth } from "./AuthContext";
import { ApiError } from "../lib/api";

export function LoginScreen() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [token, setTokenValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const value = token.trim();
    if (!value) {
      setError("Enter the local dashboard token.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(value);
      navigate("/", { replace: true });
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.status === 401
            ? "That token was rejected. Read the current token from state/dashboard_token.txt."
            : caught.message
          : "Could not reach the dashboard API.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative grid min-h-full grid-cols-1 overflow-hidden lg:grid-cols-[1.15fr_0.85fr]">
      <div className="relative hidden flex-col justify-between border-r border-ink-700 bg-ink-900/40 p-10 lg:flex xl:p-16">
        <div
          className="pointer-events-none absolute inset-0 opacity-60"
          style={{
            backgroundImage:
              "linear-gradient(rgba(126,139,155,0.06) 1px, transparent 1px), linear-gradient(90deg, rgba(126,139,155,0.06) 1px, transparent 1px)",
            backgroundSize: "48px 48px",
            maskImage: "radial-gradient(circle at 25% 30%, black, transparent 75%)",
          }}
        />
        <div className="relative">
          <div className="flex items-center gap-3">
            <div className="flex h-9 w-9 items-center justify-center border border-gold-500/50 bg-gold-500/10">
              <TerminalSquare className="h-4 w-4 text-gold-300" aria-hidden />
            </div>
            <div>
              <p className="font-display text-sm font-semibold uppercase tracking-[0.22em]">TradingAgents</p>
              <p className="text-[10px] uppercase tracking-[0.2em] text-mist-600">Local control surface</p>
            </div>
          </div>

          <h1 className="mt-20 max-w-xl font-display text-4xl font-semibold leading-tight tracking-tight text-mist-100 xl:text-5xl">
            Supervise the PAPER book.
            <span className="block text-gold-400">Never trust a green number.</span>
          </h1>
          <p className="mt-5 max-w-md text-sm leading-relaxed text-mist-500">
            Account, positions, EA health and the independent risk governor in one loopback-only console.
            Research surfaces stay clearly separated from the live path.
          </p>
        </div>

        <div className="relative grid max-w-xl gap-3 sm:grid-cols-3">
          {[
            ["MT5", "XAUUSD demo", "read-only account view"],
            ["EA bridge", "127.0.0.1:8765", "PAPER execution only"],
            ["Gate", "7 / 9 failed", "no strategy promoted"],
          ].map(([label, value, detail]) => (
            <div key={label} className="border border-ink-700/80 bg-ink-950/40 p-3">
              <p className="label">{label}</p>
              <p className="mt-1 font-mono text-xs text-mist-200">{value}</p>
              <p className="mt-1 text-[10px] leading-relaxed text-mist-600">{detail}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-center px-5 py-12 sm:px-10">
        <div className="w-full max-w-sm animate-rise">
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <div className="flex h-9 w-9 items-center justify-center border border-gold-500/50 bg-gold-500/10">
              <TerminalSquare className="h-4 w-4 text-gold-300" aria-hidden />
            </div>
            <div>
              <p className="font-display text-sm font-semibold uppercase tracking-[0.22em]">TradingAgents</p>
              <p className="text-[10px] uppercase tracking-[0.2em] text-mist-600">Local control surface</p>
            </div>
          </div>

          <p className="eyebrow">Authenticate</p>
          <h2 className="mt-2 font-display text-2xl font-semibold tracking-tight">Unlock the console</h2>
          <p className="mt-2 text-sm leading-relaxed text-mist-500">
            Use the bearer token generated on this machine. It never leaves the loopback interface.
          </p>

          <form className="mt-7 space-y-4" onSubmit={onSubmit}>
            <label className="block">
              <span className="label">Dashboard token</span>
              <div className="relative mt-2">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-mist-600" aria-hidden />
                <input
                  type="password"
                  className="input pl-9"
                  value={token}
                  onChange={(event) => setTokenValue(event.target.value)}
                  placeholder="Paste the local token"
                  autoComplete="current-password"
                  autoFocus
                  aria-invalid={Boolean(error)}
                  aria-describedby={error ? "login-error" : undefined}
                />
              </div>
            </label>

            {error ? (
              <p id="login-error" role="alert" className="border border-signal-down/35 bg-signal-down/5 px-3 py-2 text-xs leading-relaxed text-signal-down">
                {error}
              </p>
            ) : null}

            <button type="submit" className="btn btn-primary w-full" disabled={busy}>
              {busy ? "Checking…" : "Enter console"}
            </button>
          </form>

          <button
            type="button"
            className="mt-5 flex items-center gap-2 text-[11px] uppercase tracking-[0.12em] text-mist-500 hover:text-mist-300"
            onClick={() => setShowHelp((value) => !value)}
            aria-expanded={showHelp}
          >
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
            Where is the token?
          </button>
          {showHelp ? (
            <div className="mt-3 border border-ink-700 bg-ink-900/60 p-3 text-xs leading-relaxed text-mist-500">
              <p>
                The server creates <code className="mono text-gold-300">state/dashboard_token.txt</code> on first run.
                Open that file and paste the single line here. Do not commit it or share it.
              </p>
              <p className="mt-2">
                Start the API with <code className="mono text-mist-300">python main.py dashboard</code>.
              </p>
            </div>
          ) : null}

          <div className="mt-10 border-t border-ink-700 pt-4">
            <p className="text-[10px] leading-relaxed text-mist-600">
              Session-only token storage. No telemetry, no external assets, no remote access. If the API is down,
              start it from the project root.
            </p>
          </div>
        </div>
      </div>
    </main>
  );
}
