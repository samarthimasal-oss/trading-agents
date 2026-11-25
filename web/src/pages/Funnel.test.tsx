import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { FunnelPage } from "./Funnel";
import { jsonResponse, renderWithProviders } from "../test/helpers";

interface MockRoute {
  match: (url: string, init?: RequestInit) => boolean;
  body: unknown;
}

function mockApi(routes: MockRoute[]) {
  const urls: string[] = [];
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    urls.push(url);
    const route = routes.find((candidate) => candidate.match(url, init));
    if (!route) {
      return jsonResponse({ error: { code: "not_found", message: `no mock for ${url}` } }, 404);
    }
    return jsonResponse(route.body);
  });
  vi.stubGlobal("fetch", mock);
  return urls;
}

const PAYLOAD = {
  available: true,
  generated_at: 1794600000,
  window_hours: 24,
  since: 1794513600,
  symbol: null,
  latest_ts: 1794599000,
  evaluations: 16,
  stages: [
    { key: "signal", label: "Signals generated", reached: 4, passed: 3, rejected: 1, skipped: 0, outcomes: { pass: 3, reject: 1 } },
    { key: "agents", label: "Agent votes", reached: 3, passed: 3, rejected: 0, skipped: 0, outcomes: { pass: 3 } },
    { key: "kronos", label: "Kronos forecast", reached: 3, passed: 2, rejected: 0, skipped: 1, outcomes: { pass: 2, skip: 1 } },
    { key: "gemini", label: "Gemini review", reached: 2, passed: 2, rejected: 0, skipped: 0, outcomes: { pass: 1, advisory: 1 } },
    { key: "risk", label: "Risk gate", reached: 2, passed: 1, rejected: 1, skipped: 0, outcomes: { pass: 1, reject: 1 } },
    { key: "execution", label: "Execution", reached: 1, passed: 1, rejected: 0, skipped: 0, outcomes: { filled: 1 } },
    {
      key: "closed",
      label: "Closed trades",
      reached: 2,
      passed: 1,
      rejected: 1,
      skipped: 0,
      outcomes: {},
      available: true,
      win_rate: 50,
      net: 42.5,
    },
  ],
  rejections: [
    { stage: "signal", reason: "no consensus (strength +0.10)", count: 1 },
    { stage: "risk", reason: "daily loss lockout", count: 1 },
  ],
  by_symbol: [{ symbol: "XAUUSD", signals: 3, executed: 1, rejected: 1, closed: 2 }],
  orders: { filled: 1, rejected: 2, closed: 2, partial_closes: 1, sl_modified: 4 },
  note: "stage counts are written by the runner; closed trades come from MT5 deal history",
};

describe("FunnelPage", () => {
  it("renders every recorded stage, rejection reason and closed-trade result", async () => {
    mockApi([{ match: (url) => url.includes("/funnel?hours=24"), body: PAYLOAD }]);

    renderWithProviders(<FunnelPage />);

    expect(await screen.findByText("Signals generated")).toBeInTheDocument();

    const signal = screen.getByLabelText("Signals generated stage");
    expect(within(signal).getByText("4")).toBeInTheDocument();
    expect(within(signal).getByText("3 passed")).toBeInTheDocument();

    const kronos = screen.getByLabelText("Kronos forecast stage");
    expect(within(kronos).getByText("1 skip")).toBeInTheDocument();

    const closed = screen.getByLabelText("Closed trades stage");
    expect(within(closed).getByText("1 wins · 1 losses")).toBeInTheDocument();
    expect(within(closed).getByText("win rate 50.0%")).toBeInTheDocument();
    expect(within(closed).getByText("net $42.50")).toBeInTheDocument();

    expect(screen.getByText("no consensus (strength +0.10)")).toBeInTheDocument();
    expect(screen.getByText("daily loss lockout")).toBeInTheDocument();
    expect(screen.getByText("XAUUSD")).toBeInTheDocument();
    expect(screen.getByText("JOURNAL ONLINE")).toBeInTheDocument();
    expect(screen.getByText(/16 stage rows/)).toBeInTheDocument();
    expect(screen.getByText(PAYLOAD.note)).toBeInTheDocument();
  });

  it("switches the window and refetches with the selected hours", async () => {
    const urls = mockApi([
      { match: (url) => url.includes("/funnel?hours=24"), body: PAYLOAD },
      {
        match: (url) => url.includes("/funnel?hours=168"),
        body: { ...PAYLOAD, window_hours: 168, evaluations: 42, note: "7-day window" },
      },
    ]);
    const user = userEvent.setup();

    renderWithProviders(<FunnelPage />);

    expect(await screen.findByText("Signals generated")).toBeInTheDocument();
    const week = screen.getByRole("button", { name: "7d" });
    expect(week).toHaveAttribute("aria-pressed", "false");

    await user.click(week);

    await waitFor(() => {
      expect(urls.some((url) => url.includes("/funnel?hours=168"))).toBe(true);
    });
    expect(await screen.findByText(/42 stage rows/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute("aria-pressed", "true");
  });

  it("states that no funnel rows exist instead of inventing metrics", async () => {
    mockApi([
      {
        match: (url) => url.includes("/funnel?hours=24"),
        body: {
          ...PAYLOAD,
          evaluations: 0,
          latest_ts: null,
          stages: PAYLOAD.stages.map((stage) => ({
            ...stage,
            reached: 0,
            passed: 0,
            rejected: 0,
            skipped: 0,
            outcomes: {},
            ...(stage.key === "closed" ? { win_rate: null, net: 0 } : {}),
          })),
          rejections: [],
          by_symbol: [],
          orders: { filled: 0, rejected: 0, closed: 0, partial_closes: 0, sl_modified: 0 },
          note: "no funnel rows recorded yet: the upgraded runner writes one stage row per symbol per closed bar",
        },
      },
    ]);

    renderWithProviders(<FunnelPage />);

    const notes = await screen.findAllByText(/no funnel rows recorded yet/);
    expect(notes.length).toBeGreaterThan(0);
    expect(screen.getByText(/Nothing is estimated in the meantime/)).toBeInTheDocument();
    expect(screen.getByText("No rejections in this window")).toBeInTheDocument();
    expect(screen.getByText("No symbol activity recorded")).toBeInTheDocument();
  });

  it("surfaces an unreadable journal as an explicit error, not zeros", async () => {
    mockApi([
      {
        match: (url) => url.includes("/funnel?hours=24"),
        body: {
          ...PAYLOAD,
          available: false,
          evaluations: 0,
          note: "the runner journal does not exist yet",
        },
      },
    ]);

    renderWithProviders(<FunnelPage />);

    expect(await screen.findByText("The runner journal is not readable")).toBeInTheDocument();
    expect(screen.getByText("JOURNAL OFFLINE")).toBeInTheDocument();
    expect(screen.getByText("the runner journal does not exist yet")).toBeInTheDocument();
  });
});
