import type {
  Account,
  ApiErrorBody,
  BacktestsPayload,
  Committee,
  Decision,
  EquityPoint,
  Health,
  JournalEvent,
  LearningPayload,
  Order,
  Overview,
  Position,
  RiskSnapshot,
  RunnerStatus,
  SettingsPayload,
  Snapshot,
  StrategyList,
  SystemInfo,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "ta.dashboard.token";
const UNAUTHORIZED_EVENT = "ta:unauthorized";

let memoryToken: string | null = null;

export class ApiError extends Error {
  status: number;
  code: string;
  detail?: unknown;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function getToken(): string | null {
  if (memoryToken) return memoryToken;
  memoryToken = storage()?.getItem(TOKEN_KEY) ?? null;
  return memoryToken;
}

export function setToken(token: string | null): void {
  memoryToken = token;
  const store = storage();
  if (!store) return;
  if (token) store.setItem(TOKEN_KEY, token);
  else store.removeItem(TOKEN_KEY);
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  auth?: boolean;
  idempotent?: boolean;
  signal?: AbortSignal;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? (options.body === undefined ? "GET" : "POST")).toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.auth !== false) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  if (method !== "GET" && method !== "HEAD") {
    headers["X-Dashboard-Request"] = "1";
    if (options.idempotent !== false) headers["Idempotency-Key"] = newIdempotencyKey();
  }

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "The dashboard API is unreachable.");
  }

  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json().catch(() => null)
    : await response.text().catch(() => "");

  if (!response.ok) {
    const body = payload as ApiErrorBody | null;
    const code = body?.error?.code ?? `http_${response.status}`;
    const message = body?.error?.message ?? `Request failed with status ${response.status}.`;
    if (response.status === 401 && options.auth !== false) {
      setToken(null);
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(response.status, code, message, body?.error?.detail);
  }

  return payload as T;
}

export const api = {
  onUnauthorized(handler: () => void): () => void {
    window.addEventListener(UNAUTHORIZED_EVENT, handler);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
  },

  login: (token: string) =>
    apiFetch<{ ok: boolean; token_type: string }>("/auth/login", {
      method: "POST",
      body: { token },
      auth: false,
      idempotent: false,
    }),

  me: () => apiFetch<{ authenticated: boolean; mode: string | null; token_file: string }>("/auth/me"),

  health: () => apiFetch<Health>("/health", { auth: false }),

  overview: () => apiFetch<Overview>("/overview"),

  account: () => apiFetch<Account>("/account"),
  positions: () => apiFetch<{ positions: Position[]; count: number }>("/positions"),
  orders: () => apiFetch<{ orders: Order[]; count: number }>("/orders"),

  decisions: (limit = 100, symbol?: string) =>
    apiFetch<{ decisions: Decision[]; count: number }>(
      `/decisions?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}`,
    ),
  events: (limit = 100, kind?: string) =>
    apiFetch<{ events: JournalEvent[]; count: number }>(
      `/events?limit=${limit}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`,
    ),
  equity: (limit = 500) =>
    apiFetch<{ equity: EquityPoint[]; count: number }>(`/equity?limit=${limit}`),

  risk: () => apiFetch<RiskSnapshot>("/risk"),
  emergencyClose: (dryRun: boolean, confirm: string) =>
    apiFetch<{ dry_run: boolean; positions: number; closed: number; symbols: string[]; message: string; replayed?: boolean }>(
      "/risk/emergency-close",
      { method: "POST", body: { dry_run: dryRun, confirm } },
    ),
  resetGuardian: () =>
    apiFetch<{ ok: boolean }>("/risk/reset-guardian", { method: "POST", body: {} }),

  runner: () => apiFetch<{ runner: RunnerStatus }>("/runner"),
  startRunner: () => apiFetch<{ runner: RunnerStatus; mode: string; replayed?: boolean }>("/runner/start", {
    method: "POST",
    body: { mode: "dry-run" },
  }),
  stopRunner: () => apiFetch<{ runner: RunnerStatus }>("/runner/stop", { method: "POST", body: {} }),

  settings: () => apiFetch<SettingsPayload>("/settings"),
  updateSettings: (patch: Record<string, unknown>) =>
    apiFetch<SettingsPayload>("/settings", { method: "PATCH", body: patch }),

  strategies: () => apiFetch<StrategyList>("/strategies"),
  updateStrategy: (name: string, patch: { enabled?: boolean; params?: Record<string, unknown> }) =>
    apiFetch<{ strategy: unknown }>(`/strategies/${encodeURIComponent(name)}`, {
      method: "PATCH",
      body: patch,
    }),

  committee: () => apiFetch<Committee>("/committee"),

  marketOverview: (symbol: string) =>
    apiFetch<Record<string, unknown>>(`/market/overview?symbol=${encodeURIComponent(symbol)}`),

  backtests: () => apiFetch<BacktestsPayload>("/backtests"),
  learningSummary: () => apiFetch<LearningPayload>("/learning/summary"),
  learningTrades: (limit = 100) =>
    apiFetch<LearningPayload>(`/learning/trades?limit=${limit}`),

  systemInfo: () => apiFetch<SystemInfo>("/system/info"),

  snapshot: () => apiFetch<Snapshot>("/overview"),
};
