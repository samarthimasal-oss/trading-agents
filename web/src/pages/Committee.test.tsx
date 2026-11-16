import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CommitteePage } from "./Committee";
import { jsonResponse, renderWithProviders } from "../test/helpers";

function mockApi(payload: unknown) {
  const mock = vi.fn(async () => jsonResponse(payload));
  vi.stubGlobal("fetch", mock);
  return mock;
}

const BOARD = {
  available: true,
  stale: false,
  age_seconds: 42,
  mode: "PAPER",
  equity: 100149.78,
  daily_pnl: -112.4,
  symbols: {
    XAUUSD: {
      price: 3937.8,
      spread: 12,
      action: "flat",
      direction: 0,
      confidence: 0.55,
      strength: 0.2,
      regime: "range",
      agents: { trend: 0, momentum: 1, reversion: -1 },
      llm: "hold",
      llm_comment: "spread is acceptable but structure is unclear",
      kronos_direction: 1,
      kronos_summary: "mild upward drift",
      kronos_agreement: 0.67,
      kronos_runs: 3,
      why: "agents disagree",
    },
  },
  warning: null,
  reviews: [
    {
      ts: 1794671100,
      symbol: "XAUUSD",
      action: "flat",
      direction: 0,
      volume: 0,
      llm_action: "hold",
      llm_comment: "no trade until structure clears",
      reason: "no consensus",
      executed: 0,
    },
  ],
  reviews_available: true,
};

describe("CommitteePage", () => {
  it("shows a fresh board with agent votes and recorded reviews", async () => {
    mockApi(BOARD);

    renderWithProviders(<CommitteePage />);

    expect(await screen.findByText("FRESH")).toBeInTheDocument();
    expect(screen.getAllByText("XAUUSD").length).toBeGreaterThan(0);
    expect(screen.getByText("spread is acceptable but structure is unclear")).toBeInTheDocument();
    expect(screen.getByText("mild upward drift")).toBeInTheDocument();
    expect(screen.getByText("no trade until structure clears")).toBeInTheDocument();
    expect(screen.getByText("range")).toBeInTheDocument();
  });

  it("reports an absent board instead of inventing a review", async () => {
    mockApi({
      available: false,
      reason: "state/live.json has not been written by the runner",
      stale: true,
      age_seconds: null,
      symbols: {},
      reviews: [],
      reviews_available: true,
      reviews_reason: "no LLM review has been recorded in the journal",
    });

    renderWithProviders(<CommitteePage />);

    expect(await screen.findByText("NO DATA")).toBeInTheDocument();
    expect(screen.getAllByText("state/live.json has not been written by the runner").length).toBeGreaterThan(0);
    expect(screen.getByText("No committee board")).toBeInTheDocument();
    expect(screen.getByText("No review history")).toBeInTheDocument();
  });
});
