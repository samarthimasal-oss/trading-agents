import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BacktestingPage } from "./Backtesting";
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

const ARTIFACT = {
  name: "walkforward_20261112-1830.json",
  path: "docs/reports/walkforward_20261112-1830.json",
  available: true,
  created: 1794585000,
  passed: false,
  data_note: "MT5 XAUUSD M15 (12000 bars)",
  oos: {
    available: true,
    trades: 21,
    wins: 9,
    losses: 12,
    win_rate: 42.9,
    total_r: -3.2,
    expectancy_r: -0.15,
    profit_factor: 0.88,
    max_drawdown_r: 5.1,
  },
  holdout: { available: false, reason: "holdout is evaluated once and was not reached" },
  folds: [{ index: 0, metrics: { available: true, trades: 4 } }],
  gates: [
    { name: "min_trades", passed: false, detail: "21 < 40" },
    { name: "mc_dd95", skipped: true },
  ],
  gate_counts: { pass: 0, fail: 1, skip: 1 },
};

const PAYLOAD = {
  jobs: [
    {
      id: "bt-abc123",
      created: 1794580000,
      updated: 1794580300,
      status: "failed",
      params: { symbol: "XAUUSD", bars: 9000, folds: 4 },
      error: "walkforward exited with code 1; log: data error",
    },
  ],
  queue: { queued: 0, running: 0, cap: 5 },
  artifacts: [ARTIFACT],
  available: true,
  note: "walk-forward treats the strategy rules as fixed; the holdout is evaluated once",
};

function defaultRoutes(payload = PAYLOAD): MockRoute[] {
  return [
    {
      match: (url) => url.includes("/settings"),
      body: { settings: { trading: { symbol: "XAUUSD", symbols: ["XAUUSD"] } }, meta: {} },
    },
    {
      match: (url, init) => url.endsWith("/backtests") && init?.method === "POST",
      body: { job: { id: "bt-new99", status: "queued", params: {}, created: 1, updated: 1 } },
    },
    { match: (url) => url.endsWith("/backtests"), body: payload },
  ];
}

describe("BacktestingPage", () => {
  it("lists artifacts with honest gate counts and failed job errors", async () => {
    mockApi(defaultRoutes());

    renderWithProviders(<BacktestingPage />);

    expect(await screen.findByText("WALK-FORWARD FAIL")).toBeInTheDocument();
    expect(screen.getByText("0 pass")).toBeInTheDocument();
    expect(screen.getByText("1 fail")).toBeInTheDocument();
    expect(screen.getByText("1 skipped")).toBeInTheDocument();
    expect(screen.getByText(/21 < 40/)).toBeInTheDocument();
    expect(screen.getByText(/walkforward exited with code 1/i)).toBeInTheDocument();
    expect(screen.getByText(/bt-abc123/)).toBeInTheDocument();
    expect(screen.getByText(/XAUUSD · 9000 bars · 4 folds/)).toBeInTheDocument();
  });

  it("starts a walk-forward job with the form values", async () => {
    const calls = mockApi(defaultRoutes());
    const user = userEvent.setup();

    renderWithProviders(<BacktestingPage />);
    await screen.findByText("WALK-FORWARD FAIL");

    await user.click(screen.getByRole("button", { name: /start walk-forward/i }));

    await waitFor(() => {
      const post = calls.find((call) => call.url.endsWith("/backtests") && call.init?.method === "POST");
      expect(post).toBeTruthy();
      const body = JSON.parse(String(post?.init?.body));
      expect(body.symbol).toBe("XAUUSD");
      expect(body.bars).toBe(12000);
      expect(body.folds).toBe(4);
      expect(body.holdout).toBe(0.25);
      expect(body.mc_runs).toBe(2000);
      expect(body.only).toBeNull();
    });
    expect(await screen.findByText(/bt-new99 will run/i)).toBeInTheDocument();
  });

  it("blocks starting when the queue is full", async () => {
    const full = {
      ...PAYLOAD,
      queue: { queued: 4, running: 1, cap: 5 },
    };
    mockApi(defaultRoutes(full));
    const user = userEvent.setup();

    renderWithProviders(<BacktestingPage />);
    expect(await screen.findByText("queue 5/5")).toBeInTheDocument();

    const button = screen.getByRole("button", { name: /queue full/i });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(button).toBeDisabled();
  });

  it("shows the empty state when nothing has run", async () => {
    mockApi(defaultRoutes({
      jobs: [],
      queue: { queued: 0, running: 0, cap: 5 },
      artifacts: [],
      available: true,
      note: "no runs yet",
    }));

    renderWithProviders(<BacktestingPage />);

    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    expect(screen.getByText("No jobs yet")).toBeInTheDocument();
  });
});
