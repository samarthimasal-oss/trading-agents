import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "../components/Toast";
import { emptyRiskSnapshot, jsonResponse, renderWithProviders } from "../test/helpers";
import { RiskCentrePage } from "./RiskCentre";

function riskFetchMock() {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    if (url.endsWith("/risk/emergency-close")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return jsonResponse({
        dry_run: Boolean(body.dry_run),
        positions: 1,
        closed: body.dry_run ? 0 : 1,
        symbols: ["XAUUSD"],
        message: body.dry_run ? "DRY-RUN: no position was closed" : "closed 1 position(s)",
      });
    }
    if (url.endsWith("/risk")) return jsonResponse(emptyRiskSnapshot());
    if (url.endsWith("/runner")) {
      return jsonResponse({ runner: { mode: "stopped", pid: null, started: null, stale: false } });
    }
    return jsonResponse({ error: { code: "not_found", message: "route not mocked" } }, 404);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("RiskCentrePage", () => {
  it("requires the typed phrase before sending a real close-all", async () => {
    const mock = riskFetchMock();
    const user = userEvent.setup();

    renderWithProviders(
      <ToastProvider>
        <RiskCentrePage />
      </ToastProvider>,
    );

    expect(await screen.findByText("Emergency close-all")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close all positions" }));

    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Send close orders" });
    expect(confirm).toBeDisabled();

    await user.type(within(dialog).getByRole("textbox"), "CLOSE ALL");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => {
      const call = mock.mock.calls.find(
        ([url, init]) => String(url).endsWith("/risk/emergency-close") && init?.method === "POST",
      );
      expect(call).toBeTruthy();
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ dry_run: false, confirm: "CLOSE ALL" });
    });

    expect(await screen.findByText("Emergency close sent")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
