import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MarketIntelligencePage } from "./MarketIntelligence";
import { jsonResponse, renderWithProviders } from "../test/helpers";

function candles(count: number) {
  const end = 1794672000;
  return Array.from({ length: count }, (_, index) => {
    const base = 3900 + index * 0.8;
    return {
      time: end - (count - 1 - index) * 900,
      open: base - 0.3,
      high: base + 1.1,
      low: base - 1.2,
      close: base + 0.4,
      tick_volume: 100 + index,
    };
  });
}

function overview(overrides: Record<string, unknown> = {}) {
  return {
    symbol: "XAUUSD",
    timeframe: "M15",
    ts: 1794672100,
    quote: {
      symbol: "XAUUSD",
      requested_symbol: "XAUUSD",
      bid: 3937.8,
      ask: 3938.0,
      digits: 2,
      point: 0.01,
      spread_points: 20,
      time: 1794672000,
    },
    features: {
      available: true,
      last_close: 3937.9,
      ema_fast: 3935.5,
      ema_slow: 3931.0,
      rsi: 71.4,
      atr: 1.7,
      range_high: 3941.2,
      range_low: 3918.4,
      structure: "above recent range",
      regime: "up",
      bars: 239,
    },
    session: "london/new-york overlap",
    kronos: {
      available: true,
      stale: true,
      age_seconds: 7200,
      timeframe: "M15",
      direction: 1,
      move: 0.0061,
      move_next: 0.0011,
      path_high: 0.012,
      path_low: -0.004,
      base_close: 3937.9,
      base_ts: 1794664800,
      summary: "Kronos sees price UP 0.14% over ~16.0h (path +0.22%..-0.12% vs close)",
      ood: false,
      forecast_candles: Array.from({ length: 8 }, (_, index) => [
        1794672900 + index * 900,
        3938,
        3940,
        3936,
        3939,
      ]),
      source: "state/kronos_tf_cache.json",
      warning: "forecast cache is older than 4 bars; do not treat as live",
    },
    news: {
      available: true,
      stale: false,
      calendar_age_seconds: 900,
      blocked: true,
      events: [
        { ts: 1794672000, title: "Non-farm payrolls", currency: "USD", impact: "High", blocking: true },
      ],
      upcoming: [
        { ts: 1794679000, title: "CPI", currency: "USD", impact: "High" },
      ],
      warning: null,
    },
    spread: { points: 20, limit_points: 15, ok: false },
    tradable: false,
    reasons: ["spread 20.0 exceeds limit 15.0", "high-impact news window"],
    candles: candles(30),
    candle_error: null,
    ...overrides,
  };
}

function mockApi(payload: unknown) {
  const mock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/settings")) {
      return jsonResponse({ settings: { trading: { symbol: "XAUUSD", symbols: ["XAUUSD"] } }, meta: {} });
    }
    return jsonResponse(payload);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("MarketIntelligencePage", () => {
  it("shows the quote but explains why the snapshot is blocked", async () => {
    mockApi(overview());

    renderWithProviders(<MarketIntelligencePage />);

    expect(await screen.findByText("Why this is not tradable")).toBeInTheDocument();
    expect(screen.getByText("spread 20.0 exceeds limit 15.0")).toBeInTheDocument();
    expect(screen.getByText("high-impact news window")).toBeInTheDocument();
    expect(screen.getByText("20.0 pts")).toBeInTheDocument();
    expect(screen.getByText("Non-farm payrolls")).toBeInTheDocument();
    expect(screen.getByText("BLOCKED")).toBeInTheDocument();
  });

  it("labels a stale Kronos cache and keeps it out of the authority chain", async () => {
    mockApi(overview());

    renderWithProviders(<MarketIntelligencePage />);

    expect(await screen.findByText("STALE")).toBeInTheDocument();
    expect(screen.getByText(/older than 4 bars/i)).toBeInTheDocument();
    expect(screen.getByText("UP")).toBeInTheDocument();
    expect(screen.getByText(/never a trade authority/i)).toBeInTheDocument();
    expect(screen.getByText(/Kronos sees price UP 0.14%/)).toBeInTheDocument();
    expect(screen.getByText("Forecast path not drawn: the cache is stale.")).toBeInTheDocument();
    expect(screen.getAllByText("30 bars").length).toBe(2);
  });

  it("renders an honest empty state when the forecast is absent", async () => {
    mockApi(
      overview({
        kronos: { available: false, reason: "no Kronos forecast cached for XAUUSD" },
        news: { available: false, blocked: false, events: [], reason: "no news calendar on disk" },
        reasons: ["feature window unavailable"],
        tradable: false,
      }),
    );

    renderWithProviders(<MarketIntelligencePage />);

    expect(await screen.findByText("No forecast")).toBeInTheDocument();
    expect(screen.getByText("no Kronos forecast cached for XAUUSD")).toBeInTheDocument();
    expect(screen.getByText("Calendar unavailable")).toBeInTheDocument();
  });
});
