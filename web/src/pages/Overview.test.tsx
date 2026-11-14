import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { OverviewPage } from "./Overview";
import { emptyRiskSnapshot, jsonResponse, renderWithProviders } from "../test/helpers";

function overviewPayload(overrides: Record<string, unknown> = {}) {
  return {
    type: "snapshot",
    ts: 1794586000,
    account: { login: 5057915388, balance: 100149.78, equity: 100149.78, currency: "USD" },
    positions: [],
    positions_count: 0,
    orders_count: 0,
    runner: { mode: "stopped", pid: null, started: null, stale: false },
    risk: emptyRiskSnapshot(),
    bridge: { listening: false, host: "127.0.0.1", port: 8765, detail: "connection refused" },
    journal: { available: true, decisions: 0, executed: 0, filled_orders: 0, equity_points: 0 },
    health: { bridge: false, runner: null, account: true },
    committee: {
      available: false,
      reason: "state/live.json has not been written by the runner",
      stale: true,
      age_seconds: null,
      symbols: {},
    },
    decisions: [],
    events: [],
    equity: [],
    strategies: { strategies: [], research_only: true, validated: false },
    generated_at: 1794586000,
    ...overrides,
  };
}

function fetchMock(payload: unknown) {
  const mock = vi.fn(async () => jsonResponse(payload));
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("OverviewPage", () => {
  it("shows honest empty states instead of invented metrics", async () => {
    fetchMock(overviewPayload());

    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("No equity history yet")).toBeInTheDocument();
    expect(screen.getByText("Flat")).toBeInTheDocument();
    expect(screen.getByText("NO DATA")).toBeInTheDocument();
    expect(screen.getByText(/walk-forward failed 7 of 9 gates/i)).toBeInTheDocument();
    expect(screen.getAllByText(/\$100,149\.78/).length).toBeGreaterThan(0);
  });

  it("labels a stale committee board and never calls it fresh", async () => {
    fetchMock(
      overviewPayload({
        committee: {
          available: true,
          stale: true,
          age_seconds: 5400,
          mode: "LIVE",
          symbols: {
            XAUUSD: { price: 3860.1, action: "flat", why: "no consensus" },
          },
          warning: "live board is stale; start the runner for a fresh review",
        },
      }),
    );

    renderWithProviders(<OverviewPage />);

    expect(await screen.findByText("STALE")).toBeInTheDocument();
    expect(screen.getByText(/1 symbol\(s\) reviewed/)).toBeInTheDocument();
    expect(screen.getByText(/live board is stale/i)).toBeInTheDocument();
    expect(screen.queryByText("FRESH")).not.toBeInTheDocument();
  });
});
