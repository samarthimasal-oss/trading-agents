import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, getToken, setToken } from "../lib/api";
import { jsonResponse } from "../test/helpers";

function fetchMock(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return handler(url, init);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

describe("api client", () => {
  beforeEach(() => {
    setToken(null);
    window.sessionStorage.clear();
  });

  afterEach(() => {
    setToken(null);
  });

  it("posts the local token without an Authorization header", async () => {
    const mock = fetchMock(() => jsonResponse({ ok: true, token_type: "bearer" }));

    await api.login("secret-token");

    expect(mock).toHaveBeenCalledTimes(1);
    const [url, init] = mock.mock.calls[0];
    expect(url).toBe("/api/v1/auth/login");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ token: "secret-token" });
    const headers = init?.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
    expect(headers["Idempotency-Key"]).toBeUndefined();
  });

  it("attaches the bearer token to authenticated reads", async () => {
    setToken("abc123");
    const mock = fetchMock(() => jsonResponse({ positions: [], count: 0 }));

    await api.positions();

    const [, init] = mock.mock.calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer abc123");
    expect(getToken()).toBe("abc123");
  });

  it("adds a fresh idempotency key to mutations but not reads", async () => {
    setToken("abc123");
    const mock = fetchMock(() => jsonResponse({ runner: { mode: "stopped" } }));

    await api.runner();
    await api.startRunner();

    const readHeaders = mock.mock.calls[0][1]?.headers as Record<string, string>;
    const writeHeaders = mock.mock.calls[1][1]?.headers as Record<string, string>;
    expect(readHeaders["Idempotency-Key"]).toBeUndefined();
    expect(writeHeaders["Idempotency-Key"]).toMatch(/[0-9a-f-]{10,}/i);
    expect(writeHeaders["X-Dashboard-Request"]).toBe("1");
  });

  it("clears the token and emits an event on 401", async () => {
    setToken("expired");
    const handler = vi.fn();
    const off = api.onUnauthorized(handler);
    fetchMock(() =>
      jsonResponse({ error: { code: "unauthorized", message: "token is not valid" } }, 401),
    );

    await expect(api.overview()).rejects.toMatchObject({ status: 401, code: "unauthorized" });

    expect(handler).toHaveBeenCalledTimes(1);
    expect(getToken()).toBeNull();
    off();
  });

  it("surfaces structured validation errors", async () => {
    fetchMock(() =>
      jsonResponse(
        {
          error: {
            code: "validation_error",
            message: "configuration failed validation",
            detail: ["risk.max_positions: must be between 1 and 20"],
          },
        },
        422,
      ),
    );

    const error = await api.updateSettings({ "risk.max_positions": 99 }).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(422);
    expect((error as ApiError).code).toBe("validation_error");
    expect((error as ApiError).detail).toEqual(["risk.max_positions: must be between 1 and 20"]);
  });

  it("reports an unreachable API as a network error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new TypeError("fetch failed"))),
    );

    await expect(api.health()).rejects.toMatchObject({ status: 0, code: "network_error" });
  });
});
