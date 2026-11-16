import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LearningPage } from "./Learning";
import { jsonResponse, renderWithProviders } from "../test/helpers";

interface MockRoute {
  match: (url: string, init?: RequestInit) => boolean;
  body: unknown;
}

function mockApi(routes: MockRoute[]) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const route = routes.find((candidate) => candidate.match(url, init));
    if (!route) {
      return jsonResponse({ error: { code: "not_found", message: `no mock for ${url}` } }, 404);
    }
    return jsonResponse(route.body);
  });
  vi.stubGlobal("fetch", mock);
}

const SUMMARY = {
  available: true,
  data_source: "MT5 deal history on this account (last 180 days)",
  window_days: 180,
  closed_trades: 2,
  metrics: {
    closed_trades: 2,
    wins: 1,
    losses: 1,
    win_rate: 50.0,
    total_net: 42.5,
    avg_r: 0.25,
    r_known: 1,
    sessions: { london: 1, "new-york": 1 },
    symbols: { XAUUSD: 2 },
    bot_trades: 2,
    non_bot_trades: 0,
  },
  missing_fields: ["strategy", "mfe", "mae", "exit_reason"],
  attribution_note: "rows come from the terminal's deal history",
  journal_available: true,
};

const TRADES = {
  available: true,
  data_source: "MT5 deal history on this account (last 180 days)",
  trades: [
    {
      position_id: 901,
      symbol: "XAUUSD",
      side: "buy",
      volume: 0.1,
      entry_price: 3850.0,
      exit_price: 3860.0,
      net: 50.0,
      open_time: 1794400000.0,
      close_time: 1794407200.0,
      magic: 770001,
      sl: 3840.0,
      tp: 3880.0,
      session: "london",
      is_bot: true,
      realized_r: 0.5,
      risk_basis: "entry-order SL",
      digits: 2,
    },
    {
      position_id: 902,
      symbol: "XAUUSD",
      side: "sell",
      volume: 0.1,
      entry_price: 3850.0,
      exit_price: 3853.0,
      net: -30.0,
      open_time: 1794409000.0,
      close_time: 1794410000.0,
      magic: 770001,
      sl: null,
      tp: null,
      session: "new-york",
      is_bot: true,
      realized_r: null,
      risk_basis: null,
      digits: 2,
    },
  ],
  count: 2,
  limit: 200,
  missing_fields: ["strategy", "mfe", "mae", "exit_reason"],
};

function routesWith(summary: unknown, trades: unknown) {
  return [
    { match: (url: string) => url.endsWith("/learning/summary"), body: summary },
    { match: (url: string) => url.includes("/learning/trades"), body: trades },
  ];
}

describe("LearningPage", () => {
  it("derives closed trades from MT5 deal history and marks unknown R", async () => {
    mockApi(routesWith(SUMMARY, TRADES));

    renderWithProviders(<LearningPage />);

    expect(await screen.findByText("MT5 deal history on this account (last 180 days)")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("$50.00")).toBeInTheDocument();
    expect(screen.getByText("-$30.00")).toBeInTheDocument();
    expect(screen.getAllByText("3850.00").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("3853.00")).toBeInTheDocument();
    expect(screen.getByText("+0.50R")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.getByText("no entry SL")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 trades have a known R/)).toBeInTheDocument();
    expect(screen.getByText("Strategy")).toBeInTheDocument();
    expect(screen.getByText("Exit Reason")).toBeInTheDocument();
  });

  it("explains an unavailable learning store instead of showing numbers", async () => {
    mockApi(routesWith(
      {
        available: false,
        reason: "MT5 deal history unavailable: terminal offline",
        missing_fields: ["strategy", "realized_r", "mfe", "mae", "exit_reason"],
        next_step: "implement the closed-trade table and backfill from MT5 deal history",
      },
      { available: false, trades: [], reason: "MT5 deal history unavailable" },
    ));

    renderWithProviders(<LearningPage />);

    expect(await screen.findByText("Closed-trade data is not available")).toBeInTheDocument();
    expect(screen.getByText(/terminal offline/i)).toBeInTheDocument();
    expect(screen.getByText(/implement the closed-trade table/i)).toBeInTheDocument();
    expect(screen.queryByText("$50.00")).not.toBeInTheDocument();
  });

  it("shows an honest empty state when the account has no closed trades", async () => {
    mockApi(routesWith(
      {
        ...SUMMARY,
        closed_trades: 0,
        metrics: { ...SUMMARY.metrics, closed_trades: 0, wins: 0, losses: 0, win_rate: null, avg_r: null, r_known: 0, total_net: 0, bot_trades: 0, sessions: {}, symbols: {} },
        reason: "no closed trades on this account in the last 180 days",
        note: "the page will list closed trades as soon as the account books one",
      },
      { ...TRADES, trades: [], count: 0, reason: "no closed trades on this account in the last 180 days" },
    ));

    renderWithProviders(<LearningPage />);

    expect(await screen.findByText("No closed trades yet")).toBeInTheDocument();
    expect(screen.getByText(/as soon as the account books one/i)).toBeInTheDocument();
  });
});
