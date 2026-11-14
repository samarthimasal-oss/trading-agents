import { Link } from "react-router-dom";
import { ArrowRight, Construction, ShieldCheck } from "lucide-react";

import { Panel, PanelHeader } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";

const PAGES: Record<string, { title: string; purpose: string; endpoints: string[] }> = {
  "/live": {
    title: "Live Trading",
    purpose:
      "Order tickets, runner lifecycle, and a live execution tape with duplicate-prevention and restart recovery visible.",
    endpoints: ["GET /account", "GET /positions", "GET /orders", "GET /events", "POST /runner/start", "POST /runner/stop"],
  },
  "/committee": {
    title: "AI Committee",
    purpose:
      "Per-symbol agent votes, Kronos agreement, LLM review and the exact reason a trade was allowed or blocked.",
    endpoints: ["GET /committee", "GET /decisions", "GET /market/overview"],
  },
  "/strategies": {
    title: "Strategy Desk",
    purpose:
      "Five independent engines with research-only toggles, parameter provenance and promotion status. Not a live control.",
    endpoints: ["GET /strategies", "PATCH /strategies/{name}"],
  },
  "/market": {
    title: "Market Intelligence",
    purpose:
      "Candles, causal features, session context, spread checks, forecast freshness and the news calendar.",
    endpoints: ["GET /market/quote", "GET /market/candles", "GET /market/overview"],
  },
  "/backtests": {
    title: "Backtesting",
    purpose:
      "Walk-forward folds, untouched holdout, Monte Carlo, parameter sensitivity and read-only artifact inspection.",
    endpoints: ["GET /backtests"],
  },
  "/learning": {
    title: "Learning Centre",
    purpose:
      "Closed-trade attribution (strategy, R, MFE/MAE, exit reason, loss class). Blocked until the trade store exists.",
    endpoints: ["GET /learning/summary", "GET /learning/trades"],
  },
};

export function ComingSoonPage({ path }: { path: string }) {
  const page = PAGES[path] ?? {
    title: "Page",
    purpose: "This surface is not built yet.",
    endpoints: [],
  };

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />
      <Panel>
        <PanelHeader
          title={page.title}
          eyebrow="Next slice"
          action={<span className="badge badge-gold">scheduled</span>}
        />
        <div className="grid gap-6 p-6 lg:grid-cols-[1.1fr_0.9fr]">
          <div>
            <div className="flex items-center gap-2.5">
              <Construction className="h-5 w-5 text-gold-400" aria-hidden />
              <p className="font-display text-sm uppercase tracking-[0.14em] text-mist-200">Not wired yet</p>
            </div>
            <p className="mt-3 max-w-xl text-sm leading-relaxed text-mist-400">{page.purpose}</p>
            <p className="mt-4 max-w-xl text-[11px] leading-relaxed text-mist-600">
              The backend endpoints below already answer with real data. The page is intentionally absent rather
              than showing placeholder metrics that could mislead a trading decision.
            </p>
            <Link to="/" className="btn btn-ghost mt-5">
              Back to Overview
              <ArrowRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          </div>

          <div className="border border-ink-700 bg-ink-950/50 p-4">
            <p className="eyebrow">Available API</p>
            <ul className="mt-3 space-y-2">
              {page.endpoints.map((endpoint) => (
                <li key={endpoint} className="flex items-start gap-2 font-mono text-[11px] text-mist-400">
                  <ArrowRight className="mt-0.5 h-3 w-3 shrink-0 text-gold-500/70" aria-hidden />
                  {endpoint}
                </li>
              ))}
            </ul>
            <div className="hairline mt-4 flex items-start gap-2 pt-3">
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal-up" aria-hidden />
              <p className="text-[10px] leading-relaxed text-mist-600">
                Every endpoint is authenticated, loopback-only, and returns structured errors. No mutations are
                reachable from an unbuilt page.
              </p>
            </div>
          </div>
        </div>
      </Panel>
    </div>
  );
}

export { PAGES };
