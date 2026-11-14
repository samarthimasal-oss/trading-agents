import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function renderWithProviders(ui: ReactElement, initialEntries: string[] = ["/"]) {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0 },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

export function emptyRiskSnapshot() {
  return {
    limits: {
      risk_percent_per_trade: 0.5,
      fixed_lot: 0,
      max_positions: 2,
      max_open_same_symbol: 1,
      max_daily_loss_percent: 3,
      max_equity_drawdown_percent: 10,
      max_spread_points: 35,
      max_notional_leverage_per_position: 2,
      partial_close_enabled: true,
      partial_close_at_r: 1,
      partial_close_pct: 50,
      time_stop_minutes: 240,
      breakeven_atr: 1,
      trailing_atr: 1.4,
      trailing_step_atr: 0.2,
    },
    guardian: {
      available: true,
      day: "2026-11-13",
      day_start_equity: 100149.78,
      halted_day: null,
      halt_reason: "",
      halted_at: null,
      updated: 1794585000,
    },
    account: {
      login: 5057915388,
      balance: 100149.78,
      equity: 100149.78,
      currency: "USD",
    },
    open_positions: 0,
    equity: 100149.78,
    daily_loss_limit_usd: 3004.49,
    daily_pnl: 0,
    drawdown_from_peak_pct: 0,
    peak_equity: 100149.78,
    magic: 770001,
    runner: { mode: "stopped", pid: null, started: null, stale: false },
  };
}
