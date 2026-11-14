export type Nullable<T> = T | null;

export interface Account {
  login?: number;
  balance?: number;
  equity?: number;
  margin?: number;
  free_margin?: number;
  margin_level?: number;
  profit?: number;
  currency?: string;
  available?: boolean;
  reason?: string;
}

export interface Position {
  ticket: number;
  symbol: string;
  type?: number;
  side?: string;
  volume: number;
  price_open: number;
  price_current?: number;
  sl?: number;
  tp?: number;
  profit?: number;
  swap?: number;
  magic?: number;
  comment?: string;
  time?: number;
}

export interface Order {
  ticket: number;
  symbol: string;
  side?: string;
  volume_current?: number;
  price_open?: number;
  sl?: number;
  tp?: number;
  magic?: number;
}

export interface RunnerStatus {
  mode: string;
  pid: number | null;
  started: number | null;
  stale?: boolean;
  stopped_pid?: number;
}

export interface GuardianState {
  available?: boolean;
  reason?: string;
  day?: string | null;
  day_start_equity?: number | null;
  halted_day?: string | null;
  halt_reason?: string;
  halted_at?: number | null;
  updated?: number | null;
}

export interface RiskLimits {
  risk_percent_per_trade?: number;
  fixed_lot?: number;
  max_positions?: number;
  max_open_same_symbol?: number;
  max_daily_loss_percent?: number;
  max_equity_drawdown_percent?: number;
  max_spread_points?: number;
  max_notional_leverage_per_position?: number;
  partial_close_enabled?: boolean;
  partial_close_at_r?: number;
  partial_close_pct?: number;
  time_stop_minutes?: number;
  breakeven_atr?: number;
  trailing_atr?: number;
  trailing_step_atr?: number;
  min_gap_seconds_between_same_symbol_trades?: number;
  trading_hours_utc?: number[];
  dead_market_atr_ratio?: number;
  max_spread_points_by_symbol?: Record<string, number>;
}

export interface RiskSnapshot {
  limits: RiskLimits;
  guardian: GuardianState;
  account: Account;
  open_positions: number;
  equity?: number;
  day?: string;
  daily_loss_limit_usd?: number;
  daily_pnl?: number | null;
  daily_pnl_source?: string | null;
  daily_loss_used_pct?: number | null;
  drawdown_from_peak_pct?: number | null;
  peak_equity?: number | null;
  peak_equity_ts?: number | null;
  magic?: number;
  runner: RunnerStatus;
}

export interface HealthCheck {
  ok?: boolean;
  listening?: boolean;
  detail?: string;
  host?: string;
  port?: number;
  latency_ms?: number;
  mode?: string;
  pid?: number | null;
  path?: string;
}

export interface Health {
  status: "ok" | "degraded";
  ts: number;
  uptime_seconds: number;
  checks: Record<string, HealthCheck>;
}

export interface Decision {
  ts: number;
  symbol: string;
  action: string;
  direction?: number;
  volume?: number;
  sl?: number;
  tp?: number;
  strength?: number;
  confidence?: number;
  llm_action?: string;
  llm_comment?: string;
  reason?: string;
  executed?: number;
}

export interface JournalEvent {
  ts: number;
  kind: string;
  symbol?: string;
  detail?: string;
  retcode?: number;
  price?: number;
  volume?: number;
}

export interface EquityPoint {
  ts: number;
  equity: number;
  balance: number;
  margin?: number;
  floating?: number;
}

export interface CommitteeSymbol {
  price?: number;
  atr?: number;
  spread?: number;
  regime?: number | string;
  direction?: number;
  strength?: number;
  confidence?: number;
  action?: string;
  why?: string;
  agents?: Record<string, number>;
  llm?: string | null;
  llm_comment?: string | null;
  kronos_direction?: number;
  kronos_summary?: string | null;
  kronos_agreement?: number;
  kronos_runs?: number;
  news?: string;
  htf?: unknown[];
  forecast_candles?: number[][];
}

export interface Committee {
  available: boolean;
  reason?: string;
  stale?: boolean;
  age_seconds?: number | null;
  mode?: string;
  equity?: number;
  daily_pnl?: number;
  symbols: Record<string, CommitteeSymbol>;
  warning?: string | null;
}

export interface Overview {
  type: string;
  ts: number;
  account: Account;
  positions: Position[];
  positions_count: number | null;
  orders_count: number | null;
  runner: RunnerStatus;
  risk: RiskSnapshot | { available: false; reason: string };
  bridge: HealthCheck;
  journal: {
    available?: boolean;
    decisions?: number;
    executed?: number;
    filled_orders?: number;
    equity_points?: number;
  };
  health: { bridge: boolean; runner: string | null; account: boolean };
  committee: Committee;
  decisions: Decision[];
  events: JournalEvent[];
  equity: EquityPoint[];
  strategies: StrategyList | { available: false; reason: string };
  generated_at: number;
}

export interface Strategy {
  name: string;
  description: string;
  enabled: boolean;
  configured_enabled: boolean;
  weight: number;
  params: Record<string, unknown>;
  overrides: Record<string, unknown>;
  research_only: boolean;
}

export interface StrategyList {
  strategies: Strategy[];
  research_only: boolean;
  live_path: string;
  section: Record<string, unknown>;
  validated: boolean;
  validation_note: string;
}

export interface PublicSettings {
  trading: Record<string, unknown>;
  risk: Record<string, unknown>;
  mt5: Record<string, unknown> & {
    password_present?: boolean;
  };
  llm: Record<string, unknown> & { api_key_present?: boolean };
  kronos: Record<string, unknown>;
  news: Record<string, unknown>;
  telegram: Record<string, unknown> & { bot_token_present?: boolean };
  ea: Record<string, unknown> & { token_present?: boolean };
  strategies: Record<string, unknown>;
}

export interface SettingsPayload {
  settings: PublicSettings;
  meta: { config_path: string; restart_required: boolean; secrets: string };
}

export interface BacktestArtifact {
  name: string;
  path: string;
  available: boolean;
  reason?: string;
  created?: number;
  passed?: boolean | null;
  data_note?: string;
  oos?: Record<string, unknown>;
  holdout?: Record<string, unknown>;
  folds?: Array<Record<string, unknown>>;
  gates?: Array<{ name: string; passed?: boolean; skipped?: boolean; detail?: string; status?: string }>;
  gate_counts?: { pass: number; fail: number; skip: number };
  config?: Record<string, unknown>;
}

export interface BacktestsPayload {
  jobs: Array<Record<string, unknown>>;
  artifacts: BacktestArtifact[];
  available: boolean;
  note: string;
}

export interface LearningPayload {
  available: boolean;
  reason?: string;
  missing_fields?: string[];
  trades?: Array<Record<string, unknown>>;
}

export interface SystemInfo {
  python: string;
  platform: string;
  root: string;
  config_path: string;
  database: string;
  token_file: string;
  bridge: { host: string; port: number };
  ws_interval: number;
  static_dir: string | null;
  time: number;
}

export interface Snapshot {
  type: "snapshot";
  ts: number;
  account: Account;
  positions: Position[];
  positions_count: number | null;
  orders_count: number | null;
  runner: RunnerStatus;
  risk: RiskSnapshot | { available: false; reason: string };
  bridge: HealthCheck;
  journal: Record<string, unknown>;
  health: { bridge: boolean; runner: string | null; account: boolean };
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    detail?: unknown;
  };
}
