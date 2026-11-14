import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AuthProvider } from "./AuthContext";
import { LoginScreen } from "./LoginScreen";
import { jsonResponse } from "../test/helpers";

function fetchMock(handler: (url: string, init?: RequestInit) => Response) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return handler(url, init);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("LoginScreen", () => {
  it("rejects an invalid token without storing it", async () => {
    fetchMock(() =>
      jsonResponse({ error: { code: "unauthorized", message: "the dashboard token is not valid" } }, 401),
    );
    const user = userEvent.setup();

    render(
      <MemoryRouter>
        <AuthProvider>
          <LoginScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Dashboard token"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Enter console" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/token was rejected/i);
    expect(window.sessionStorage.getItem("ta.dashboard.token")).toBeNull();
  });

  it("stores the token after a successful login and verifies the session", async () => {
    const mock = fetchMock((url) => {
      if (url.endsWith("/auth/me")) {
        return jsonResponse({ authenticated: true, mode: "stopped", token_file: "state/dashboard_token.txt" });
      }
      return jsonResponse({ ok: true, token_type: "bearer", scope: "local" });
    });
    const user = userEvent.setup();

    render(
      <MemoryRouter>
        <AuthProvider>
          <LoginScreen />
        </AuthProvider>
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Dashboard token"), "good-token");
    await user.click(screen.getByRole("button", { name: "Enter console" }));

    await waitFor(() => {
      expect(window.sessionStorage.getItem("ta.dashboard.token")).toBe("good-token");
    });
    await waitFor(() => {
      expect(mock.mock.calls.some(([url]) => String(url).endsWith("/auth/me"))).toBe(true);
    });
  });
});
