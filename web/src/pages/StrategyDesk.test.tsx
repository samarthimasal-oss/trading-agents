import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { StrategyDeskPage } from "./StrategyDesk";
import { jsonResponse, renderWithProviders } from "../test/helpers";

interface MockRoute {
  match: (url: string, init?: RequestInit) => boolean;
  body: unknown;
  status?: number;
}

function mockApi(routes: MockRoute[]) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const route = routes.find((candidate) => candidate.match(url, init));
    if (!route) {
      return jsonResponse({ error: { code: "not_found", message: `no mock for ${url}` } }, 404);
    }
    return jsonResponse(route.body, route.status ?? 200);
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const STRATEGIES = {
  strategies: [
    {
      name: "trend",
      description: "EMA fast/slow crossover with slope quality",
      enabled: true,
      configured_enabled: true,
      weight: 1,
      params: { ema_fast: 20, ema_slow: 50 },
      editable_params: ["ema_fast", "ema_slow"],
      overrides: {},
      research_only: true,
    },
  ],
  research_only: true,
  live_path: "legacy consensus runner; these engines are replay-only",
  section: { enabled: true },
  validated: false,
  validation_note: "walk-forward gate failed on 2026-11-12; no engine is promoted",
};

const SIGNAL = {
  strategy: "trend",
  symbol: "XAUUSD",
  timeframe: "M15",
  direction: 1,
  entry: 3937.8,
  stop: 3929.82,
  target: 3953.77,
  rr: 2,
  confidence: 0.8,
  expires_at: 1794672900,
  setup_id: "trend-1",
  evidence: ["ema stack up"],
  invalidators: ["close below 3929.82"],
  regime: "up",
  session: "overlap",
  bar_time: 1794671100,
  status: "selected",
  reject_reason: null,
  score: 0.9,
};

const SCAN = {
  available: true,
  symbol: "XAUUSD",
  timeframe: "M15",
  generated_at: 1794672000,
  last_closed_bar: 1794671100,
  forming_bar_time: 1794672000,
  bars: 399,
  closed_bars_only: true,
  spread_points: 12,
  spread_source: "live quote",
  spread_assumed_zero: false,
  engines: [{ name: "trend", enabled: true, proposals: 1 }],
  proposals: [SIGNAL],
  selected: [SIGNAL],
  rejections: { below_min_rr: 1 },
  config_source: "config.json defaults",
  research_only: true,
};

const REPLAY = {
  available: true,
  symbol: "XAUUSD",
  timeframe: "M15",
  generated_at: 1794672000,
  last_closed_bar: 1794671100,
  forming_bar_time: 1794672000,
  bars: 1199,
  closed_bars_only: true,
  data_note: "MT5 XAUUSD M15: 1199 closed bars; in-sample, next-bar fills",
  spread_points: 12,
  slippage_points: 2,
  spread_source: "live quote",
  proposals: 10,
  selected: 3,
  rejections: {},
  combined: {
    trades: 21,
    wins: 9,
    losses: 12,
    win_rate: 42.9,
    total_r: -3.2,
    expectancy_r: -0.15,
    profit_factor: 0.88,
    max_drawdown_r: 5.1,
    exit_reasons: { stop: 12 },
  },
  by_strategy: {
    trend: {
      strategy: "trend",
      trades: 21,
      wins: 9,
      losses: 12,
      win_rate: 42.9,
      total_r: -3.2,
      expectancy_r: -0.15,
      profit_factor: 0.88,
      avg_mfe_r: 0.9,
      avg_mae_r: 0.7,
      exit_reasons: { stop: 12 },
    },
  },
  by_session: {
    london: {
      strategy: "london",
      trades: 8,
      win_rate: 50,
      total_r: 1.2,
      expectancy_r: 0.15,
      profit_factor: 1.2,
    },
  },
  equity_curve: [0, -1, -2, -1.5, -3.2],
  trades: [
    {
      strategy: "trend",
      symbol: "XAUUSD",
      direction: 1,
      entry_time: 1794600000,
      exit_time: 1794603600,
      entry: 3920,
      exit: 3912,
      r_net: -1,
      mfe_r: 0.4,
      mae_r: 1.1,
      bars_held: 16,
      exit_reason: "stop",
      session: "london",
      regime: "up",
      confidence: 0.8,
    },
  ],
  trades_total: 21,
  research_only: true,
  validation: {
    walkforward_passed: false,
    note: "in-sample replay only; the 2026-11-12 walk-forward gate failed, so no engine is promoted",
  },
};

function defaultRoutes(): MockRoute[] {
  return [
    {
      match: (url) => url.includes("/settings"),
      body: { settings: { trading: { symbol: "XAUUSD", symbols: ["XAUUSD"] } }, meta: {} },
    },
    {
      match: (url, init) => url.includes("/strategies/signals") && init?.method === "POST",
      body: SCAN,
    },
    {
      match: (url, init) => url.includes("/strategies/replay") && init?.method === "POST",
      body: REPLAY,
    },
    {
      match: (url, init) => /\/strategies\/[a-z_]+$/.test(url) && init?.method === "PATCH",
      body: { strategy: { ...STRATEGIES.strategies[0], enabled: false } },
    },
    { match: (url) => url.endsWith("/strategies"), body: STRATEGIES },
  ];
}

describe("StrategyDeskPage", () => {
  it("labels the desk as research only and saves an engine toggle", async () => {
    const calls = mockApi(defaultRoutes());
    const user = userEvent.setup();

    renderWithProviders(<StrategyDeskPage />);

    expect(await screen.findByText("Research desk — not a promoted engine")).toBeInTheDocument();
    expect(screen.getByText(/walk-forward gate failed on 2026-11-12/i)).toBeInTheDocument();

    await user.click(screen.getByLabelText("Enable trend for research scans"));
    await user.click(screen.getByRole("button", { name: /save/i }));

    await waitFor(() => {
      const patch = calls.find((call) => call.init?.method === "PATCH");
      expect(patch).toBeTruthy();
      expect(patch?.url).toContain("/strategies/trend");
      expect(String(patch?.init?.body)).toContain('"enabled":false');
    });
  });

  it("scans closed bars and explains every proposal and rejection", async () => {
    mockApi(defaultRoutes());
    const user = userEvent.setup();

    renderWithProviders(<StrategyDeskPage />);
    await screen.findByText("Research desk — not a promoted engine");

    await user.click(screen.getByRole("button", { name: /^scan$/i }));

    expect(await screen.findByText("ema stack up")).toBeInTheDocument();
    expect(screen.getByText("excluded from the scan")).toBeInTheDocument();
    expect(screen.getByText("399")).toBeInTheDocument();
    expect(screen.getByText("below_min_rr · 1")).toBeInTheDocument();
    expect(screen.getAllByText("selected").length).toBeGreaterThan(0);
  });

  it("marks an in-sample replay as not validated", async () => {
    mockApi(defaultRoutes());
    const user = userEvent.setup();

    renderWithProviders(<StrategyDeskPage />);
    await screen.findByText("Research desk — not a promoted engine");

    await user.click(screen.getByRole("button", { name: /run replay/i }));

    expect(await screen.findByText("WALK-FORWARD FAILED")).toBeInTheDocument();
    expect(screen.getByText(/in-sample replay only/i)).toBeInTheDocument();
    expect(screen.getAllByText("-3.20R").length).toBeGreaterThan(0);
    expect(screen.getByText("Stop")).toBeInTheDocument();
  });
});
