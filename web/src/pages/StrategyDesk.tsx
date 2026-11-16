import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Beaker,
  FlaskConical,
  Gauge,
  Play,
  RefreshCw,
  SlidersHorizontal,
  Sparkles,
  TrendingDown,
  TrendingUp,
  TriangleAlert,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, ApiError } from "../lib/api";
import type { ReplayMetrics, Strategy, StrategySignal } from "../lib/types";
import { localTime, num, rMultiple, signed, titleCase, toneForNumber } from "../lib/format";
import { Badge, EmptyState, ErrorNote, LoadingBlock, Panel, PanelHeader } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { useToast } from "../components/Toast";
import { useSymbols } from "../hooks/useSymbols";

const TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"];

function SideIcon({ direction }: { direction: number | undefined }) {
  if (direction === 1) return <TrendingUp className="h-3.5 w-3.5 text-signal-up" aria-label="long" />;
  if (direction === -1) return <TrendingDown className="h-3.5 w-3.5 text-signal-down" aria-label="short" />;
  return <span className="inline-block h-2 w-2 rounded-full bg-mist-600" aria-label="flat" />;
}

function MetricTile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: string;
  tone?: string;
  hint?: string;
}) {
  return (
    <div className="border border-ink-700/70 bg-ink-950/40 px-3 py-2.5">
      <p className="label">{label}</p>
      <p className={`mt-1 font-mono text-lg tabular-nums ${tone ?? "text-mist-100"}`}>{value}</p>
      {hint ? <p className="mt-0.5 text-[10px] text-mist-600">{hint}</p> : null}
    </div>
  );
}

// ------------------------------------------------------------------ engine card
function EngineCard({
  strategy,
  onSaved,
}: {
  strategy: Strategy;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(strategy.enabled);
  const [weight, setWeight] = useState(String(strategy.weight));
  const editable = strategy.editable_params ?? [];
  const [params, setParams] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const key of editable) initial[key] = String(strategy.params[key] ?? "");
    return initial;
  });
  const [formError, setFormError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: (patch: { enabled?: boolean; params?: Record<string, unknown> }) =>
      api.updateStrategy(strategy.name, patch),
    onSuccess: () => {
      toast.push("success", `${titleCase(strategy.name)} updated`, "Research override written. The legacy runner is untouched.");
      onSaved();
    },
    onError: (error) => {
      const detail = error instanceof ApiError ? error.message : String(error);
      setFormError(detail);
      toast.push("error", "Override rejected", detail);
    },
  });

  const submit = () => {
    setFormError(null);
    const patch: { enabled?: boolean; params?: Record<string, unknown> } = {};
    if (enabled !== strategy.enabled) patch.enabled = enabled;
    const changed: Record<string, number> = {};
    const weightNumber = Number(weight);
    if (!Number.isFinite(weightNumber) || weightNumber < 0 || weightNumber > 10) {
      setFormError("Weight must be a number between 0 and 10.");
      return;
    }
    if (Math.abs(weightNumber - Number(strategy.weight)) > 1e-9) changed.weight = weightNumber;
    for (const key of editable) {
      const raw = params[key];
      if (raw === undefined || raw === "") continue;
      const value = Number(raw);
      if (!Number.isFinite(value) || value <= 0) {
        setFormError(`${key} must be a positive number.`);
        return;
      }
      if (Math.abs(value - Number(strategy.params[key])) > 1e-9) changed[key] = value;
    }
    if (Object.keys(changed).length) patch.params = changed;
    if (patch.enabled === undefined && !patch.params) {
      toast.push("info", "Nothing to save", "No field differs from the current research configuration.");
      return;
    }
    save.mutate(patch);
  };

  const overrideCount = Object.keys(strategy.overrides ?? {}).length;

  return (
    <Panel className="flex flex-col">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-ink-700/70 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-display text-sm font-semibold tracking-[0.08em]">{titleCase(strategy.name)}</h3>
            {overrideCount ? <Badge tone="gold">OVERRIDE</Badge> : null}
            {!strategy.enabled ? <Badge tone="idle">DISABLED</Badge> : null}
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-mist-500">{strategy.description}</p>
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-[10px] uppercase tracking-[0.12em] text-mist-500">
          <input
            type="checkbox"
            className="h-3.5 w-3.5 accent-gold-500"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
            aria-label={`Enable ${strategy.name} for research scans`}
          />
          Enabled
        </label>
      </div>

      <div className="grid grid-cols-2 gap-3 border-b border-ink-700/60 px-4 py-3">
        <label className="block">
          <span className="label">Weight</span>
          <input
            type="number"
            min={0}
            max={10}
            step={0.05}
            value={weight}
            onChange={(event) => setWeight(event.target.value)}
            className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
          />
        </label>
        <div>
          <p className="label">Configured</p>
          <p className="mt-1.5 font-mono text-xs text-mist-400">
            {strategy.configured_enabled ? "enabled" : "disabled"} in config.json
          </p>
        </div>
      </div>

      <div className="flex-1 px-4 py-3">
        <p className="label mb-2">Numeric parameters</p>
        {editable.length === 0 ? (
          <p className="text-[11px] text-mist-600">This engine has no editable numeric defaults.</p>
        ) : (
          <div className="grid max-h-[240px] grid-cols-2 gap-2 overflow-y-auto pr-1">
            {editable.map((key) => (
              <label key={key} className="block">
                <span className="block truncate font-mono text-[10px] text-mist-500" title={key}>
                  {key}
                </span>
                <input
                  type="number"
                  step="any"
                  value={params[key] ?? ""}
                  onChange={(event) => setParams((current) => ({ ...current, [key]: event.target.value }))}
                  className="mt-0.5 w-full border border-ink-600 bg-ink-950/70 px-2 py-1 font-mono text-[11px] text-mist-100"
                />
              </label>
            ))}
          </div>
        )}
        {formError ? <p className="mt-2 text-[11px] text-signal-down">{formError}</p> : null}
      </div>

      <div className="flex items-center justify-between gap-2 border-t border-ink-700/60 px-4 py-2.5">
        <span className="text-[10px] text-mist-600">
          {overrideCount ? `${overrideCount} override group${overrideCount === 1 ? "" : "s"} on disk` : "no override on disk"}
        </span>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={submit}
          disabled={save.isPending}
        >
          {save.isPending ? <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
          Save
        </button>
      </div>
    </Panel>
  );
}

// ------------------------------------------------------------------ signal row
function SignalRow({ signal }: { signal: StrategySignal }) {
  const selected = signal.status === "selected";
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <SideIcon direction={signal.direction} />
          <span className="font-display text-xs uppercase tracking-[0.1em]">{titleCase(signal.strategy)}</span>
          <Badge tone={selected ? "ok" : "idle"}>{signal.status ?? "proposal"}</Badge>
          {signal.reject_reason ? <Badge tone="warn">{signal.reject_reason}</Badge> : null}
        </div>
        <div className="flex items-center gap-3 font-mono text-[10px] text-mist-500">
          <span>conf {num(signal.confidence * 100, 0)}%</span>
          <span>R:R {num(signal.rr, 2)}</span>
          <span>score {num(signal.score, 3)}</span>
        </div>
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2 font-mono text-[11px]">
        <span className="text-mist-500">entry <span className="text-mist-200">{num(signal.entry)}</span></span>
        <span className="text-mist-500">stop <span className="text-signal-down">{num(signal.stop)}</span></span>
        <span className="text-mist-500">target <span className="text-signal-up">{num(signal.target)}</span></span>
      </div>
      {signal.evidence.length ? (
        <p className="mt-1.5 text-[10px] leading-relaxed text-mist-600">
          {signal.evidence.join(" · ")}
        </p>
      ) : null}
    </li>
  );
}

// ------------------------------------------------------------------ replay view
function ReplayMetricsGrid({ metrics }: { metrics: ReplayMetrics }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <MetricTile label="Trades" value={String(metrics.trades)} hint={`${metrics.wins ?? 0}W / ${metrics.losses ?? 0}L`} />
      <MetricTile label="Total R" value={rMultiple(metrics.total_r)} tone={toneForNumber(metrics.total_r)} />
      <MetricTile label="Expectancy" value={rMultiple(metrics.expectancy_r)} tone={toneForNumber(metrics.expectancy_r)} />
      <MetricTile
        label="Profit factor"
        value={metrics.profit_factor === null ? "∞ / n/a" : num(metrics.profit_factor, 2)}
        hint={metrics.profit_factor === null ? "no losing trade in sample" : undefined}
      />
      <MetricTile label="Win rate" value={`${num(metrics.win_rate, 1)}%`} />
      <MetricTile label="Max DD" value={rMultiple(-Math.abs(metrics.max_drawdown_r ?? 0))} tone="text-signal-down" />
    </div>
  );
}

export function StrategyDeskPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const symbols = useSymbols();
  const [symbol, setSymbol] = useState("XAUUSD");
  const [timeframe, setTimeframe] = useState("M15");
  const [scanBars, setScanBars] = useState(400);
  const [replayBars, setReplayBars] = useState(1200);

  const listQuery = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });

  const scan = useMutation({
    mutationFn: () => api.strategySignals({ symbol, timeframe, bars: scanBars }),
    onError: (error) => {
      toast.push("error", "Signal scan failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  const replay = useMutation({
    mutationFn: () => api.strategyReplay({ symbol, timeframe, bars: replayBars }),
    onError: (error) => {
      toast.push("error", "Replay failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  const strategies = listQuery.data?.strategies ?? [];
  const scanData = scan.data;
  const replayData = replay.data;
  const equity = useMemo(
    () => (replayData?.equity_curve ?? []).map((value, index) => ({ index, r: Number(value) })),
    [replayData],
  );
  const strategyRows = useMemo(
    () => Object.entries(replayData?.by_strategy ?? {}),
    [replayData],
  );

  if (listQuery.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel><LoadingBlock label="Reading the five research engines" /></Panel>
      </div>
    );
  }

  if (listQuery.isError) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Strategy Desk unavailable" danger />
          <div className="p-4">
            <ErrorNote
              title="Could not read the strategy configuration"
              detail={listQuery.error instanceof Error ? listQuery.error.message : String(listQuery.error)}
            />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void listQuery.refetch()}>
              Retry
            </button>
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="flex flex-wrap items-start justify-between gap-3 border border-signal-warn/35 bg-signal-warn/5 px-4 py-3">
        <div className="flex items-start gap-2.5">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-signal-warn" aria-hidden />
          <div>
            <p className="text-xs font-semibold text-signal-warn">Research desk — not a promoted engine</p>
            <p className="mt-1 max-w-3xl text-[11px] leading-relaxed text-mist-500">
              {listQuery.data?.validation_note ?? "The walk-forward gate has not passed."}
              {" "}Overrides are written to <span className="font-mono">state/strategy_overrides.json</span> and affect
              the scans and replays on this page only. The legacy runner is unchanged.
            </p>
          </div>
        </div>
        <Badge tone="gold">{listQuery.data?.research_only ? "RESEARCH ONLY" : "UNKNOWN"}</Badge>
      </div>

      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="eyebrow">Engine configuration · {strategies.length}</p>
          <span className="font-mono text-[10px] text-mist-600">{listQuery.data?.live_path}</span>
        </div>
        <div className="grid items-start gap-3 xl:grid-cols-2 2xl:grid-cols-3">
          {strategies.map((strategy) => (
            <EngineCard
              key={`${strategy.name}-${JSON.stringify(strategy.overrides)}`}
              strategy={strategy}
              onSaved={() => {
                void queryClient.invalidateQueries({ queryKey: ["strategies"] });
              }}
            />
          ))}
        </div>
      </div>

      <Panel>
        <PanelHeader
          title="Current-bar signal scan"
          eyebrow="Closed bars only · forming bar discarded"
          action={
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor="scan-symbol">Symbol</label>
              <select
                id="scan-symbol"
                className="border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              >
                {symbols.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <label className="sr-only" htmlFor="scan-timeframe">Timeframe</label>
              <select
                id="scan-timeframe"
                className="border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
                value={timeframe}
                onChange={(event) => setTimeframe(event.target.value)}
              >
                {TIMEFRAMES.map((value) => <option key={value} value={value}>{value}</option>)}
              </select>
              <label className="sr-only" htmlFor="scan-bars">Bars</label>
              <input
                id="scan-bars"
                type="number"
                min={260}
                max={1500}
                step={20}
                value={scanBars}
                onChange={(event) => setScanBars(Number(event.target.value))}
                className="w-20 border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
                title="Closed bars requested"
              />
              <button type="button" className="btn btn-primary" onClick={() => scan.mutate()} disabled={scan.isPending}>
                {scan.isPending ? <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Play className="h-3.5 w-3.5" aria-hidden />}
                {scan.isPending ? "Scanning" : "Scan"}
              </button>
            </div>
          }
        />

        {scan.isError ? (
          <div className="p-4">
            <ErrorNote
              title="Signal scan failed"
              detail={scan.error instanceof ApiError ? scan.error.message : String(scan.error)}
            />
          </div>
        ) : !scanData ? (
          <EmptyState
            icon={<Beaker className="h-6 w-6" aria-hidden />}
            title="No scan yet"
            detail="Run a scan to evaluate the enabled engines on the last closed bar. This is a research signal, not an order."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-2 border-b border-ink-700/60 p-4 sm:grid-cols-4 xl:grid-cols-6">
              <MetricTile label="Closed bars" value={String(scanData.bars)} hint={`last ${localTime(scanData.last_closed_bar)}`} />
              <MetricTile label="Forming bar" value={localTime(scanData.forming_bar_time).slice(-8)} hint="excluded from the scan" />
              <MetricTile label="Spread" value={scanData.spread_points === null ? "unavailable" : `${num(scanData.spread_points, 1)} pts`} hint={scanData.spread_assumed_zero ? "assumed zero — quote missing" : scanData.spread_source} />
              <MetricTile label="Selected" value={String(scanData.selected.length)} hint={`${scanData.proposals.length} proposal(s)`} />
              <MetricTile label="Config" value={scanData.config_source.includes("overrides") ? "with overrides" : "defaults"} hint={scanData.config_source} />
              <MetricTile label="Symbol / TF" value={`${scanData.symbol} ${scanData.timeframe}`} />
            </div>

            <div className="flex flex-wrap items-center gap-2 border-b border-ink-700/60 px-4 py-2.5">
              <span className="label">Engines</span>
              {scanData.engines.map((engine) => (
                <Badge key={engine.name} tone={!engine.enabled ? "idle" : engine.proposals ? "gold" : "ok"}>
                  {engine.name} {engine.enabled ? `· ${engine.proposals}` : "· off"}
                </Badge>
              ))}
              {Object.entries(scanData.rejections).map(([reason, count]) => (
                <Badge key={reason} tone="warn">{reason} · {count}</Badge>
              ))}
            </div>

            {scanData.proposals.length === 0 ? (
              <EmptyState
                icon={<Gauge className="h-6 w-6" aria-hidden />}
                title="No proposal on this bar"
                detail="No enabled engine found a setup at the last closed bar. That is a normal outcome, not a missing feature."
              />
            ) : (
              <ul className="divide-y divide-ink-700/50">
                {scanData.proposals.map((signal) => (
                  <SignalRow key={`${signal.strategy}-${signal.setup_id}`} signal={signal} />
                ))}
              </ul>
            )}
          </>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="In-sample replay"
          eyebrow="Same engines, next-bar fills, spread and slippage charged"
          action={
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor="replay-bars">Replay bars</label>
              <input
                id="replay-bars"
                type="number"
                min={320}
                max={1500}
                step={20}
                value={replayBars}
                onChange={(event) => setReplayBars(Number(event.target.value))}
                className="w-20 border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
                title="Closed bars requested"
              />
              <button type="button" className="btn btn-primary" onClick={() => replay.mutate()} disabled={replay.isPending}>
                {replay.isPending ? <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <FlaskConical className="h-3.5 w-3.5" aria-hidden />}
                {replay.isPending ? "Replaying" : "Run replay"}
              </button>
            </div>
          }
        />

        {replay.isError ? (
          <div className="p-4">
            <ErrorNote
              title="Replay failed"
              detail={replay.error instanceof ApiError ? replay.error.message : String(replay.error)}
            />
          </div>
        ) : !replayData ? (
          <EmptyState
            icon={<FlaskConical className="h-6 w-6" aria-hidden />}
            title="No replay yet"
            detail="A replay evaluates the engines over historical closed bars with conservative fill assumptions. Results are in-sample and do not validate an engine."
          />
        ) : (
          <div className="space-y-4 p-4">
            <div className="flex flex-wrap items-start justify-between gap-2 border border-signal-warn/35 bg-signal-warn/5 px-3 py-2.5">
              <p className="flex items-start gap-2 text-[11px] leading-relaxed text-signal-warn">
                <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                {replayData.validation.note}
              </p>
              <Badge tone="bad">WALK-FORWARD FAILED</Badge>
            </div>

            <p className="font-mono text-[10px] text-mist-600">
              {replayData.data_note} · {replayData.bars} bars · spread {num(replayData.spread_points, 1)} pts ·
              slippage {num(replayData.slippage_points, 1)} pts · {replayData.spread_source}
            </p>

            <ReplayMetricsGrid metrics={replayData.combined} />

            {equity.length > 1 ? (
              <div>
                <p className="label mb-2">Cumulative R · in-sample</p>
                <div className="h-[220px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={equity} margin={{ top: 5, right: 12, bottom: 0, left: -18 }}>
                      <defs>
                        <linearGradient id="replayR" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#d9ae62" stopOpacity={0.35} />
                          <stop offset="100%" stopColor="#d9ae62" stopOpacity={0.02} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid stroke="#1f2934" strokeDasharray="2 5" vertical={false} />
                      <XAxis dataKey="index" tick={{ fill: "#647181", fontSize: 9 }} axisLine={false} tickLine={false} minTickGap={40} />
                      <YAxis tick={{ fill: "#647181", fontSize: 9 }} axisLine={false} tickLine={false} width={46} />
                      <Tooltip
                        contentStyle={{ background: "#0b0e12", border: "1px solid #2a3644", fontSize: 11 }}
                        labelStyle={{ color: "#8b97a6" }}
                        formatter={(value: number) => [`${value.toFixed(2)}R`, "cumulative"]}
                        labelFormatter={(value) => `trade #${value}`}
                      />
                      <Area type="monotone" dataKey="r" stroke="#d9ae62" strokeWidth={1.4} fill="url(#replayR)" />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            ) : null}

            <div className="overflow-x-auto">
              <table className="w-full min-w-[680px] border-collapse">
                <thead>
                  <tr className="table-head">
                    <th className="px-3 py-2">Engine</th>
                    <th className="px-3 py-2 text-right">Trades</th>
                    <th className="px-3 py-2 text-right">Total R</th>
                    <th className="px-3 py-2 text-right">Expectancy</th>
                    <th className="px-3 py-2 text-right">Win %</th>
                    <th className="px-3 py-2 text-right">PF</th>
                    <th className="px-3 py-2 text-right">MFE / MAE</th>
                  </tr>
                </thead>
                <tbody>
                  {strategyRows.map(([name, metrics]) => (
                    <tr key={name} className="hover:bg-ink-800/40">
                      <td className="table-cell font-display text-[11px] uppercase tracking-wide">{titleCase(name)}</td>
                      <td className="table-cell text-right font-mono text-xs">{metrics.trades}</td>
                      <td className={`table-cell text-right font-mono text-xs ${toneForNumber(metrics.total_r)}`}>{rMultiple(metrics.total_r)}</td>
                      <td className={`table-cell text-right font-mono text-xs ${toneForNumber(metrics.expectancy_r)}`}>{rMultiple(metrics.expectancy_r)}</td>
                      <td className="table-cell text-right font-mono text-xs">{num(metrics.win_rate, 1)}%</td>
                      <td className="table-cell text-right font-mono text-xs">{metrics.profit_factor === null ? "∞ / n/a" : num(metrics.profit_factor, 2)}</td>
                      <td className="table-cell text-right font-mono text-xs text-mist-400">
                        {num(metrics.avg_mfe_r, 2)} / {num(metrics.avg_mae_r, 2)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {replayData.trades.length === 0 ? (
              <EmptyState
                icon={<Gauge className="h-6 w-6" aria-hidden />}
                title="No replay trades"
                detail="The engines produced no fill over this window. Widen the bar count or review the engine parameters."
              />
            ) : (
              <div>
                <p className="label mb-2">Trades · showing {replayData.trades.length} of {replayData.trades_total}</p>
                <div className="max-h-[320px] overflow-auto">
                  <table className="w-full min-w-[760px] border-collapse">
                    <thead className="sticky top-0 bg-ink-900">
                      <tr className="table-head">
                        <th className="px-3 py-2">Entry</th>
                        <th className="px-3 py-2">Engine</th>
                        <th className="px-3 py-2">Side</th>
                        <th className="px-3 py-2 text-right">R</th>
                        <th className="px-3 py-2 text-right">MFE / MAE</th>
                        <th className="px-3 py-2">Exit</th>
                        <th className="px-3 py-2">Session</th>
                      </tr>
                    </thead>
                    <tbody>
                      {replayData.trades.map((trade, index) => (
                        <tr key={`${trade.strategy}-${trade.entry_time}-${index}`} className="hover:bg-ink-800/40">
                          <td className="table-cell whitespace-nowrap font-mono text-[10px] text-mist-500">{localTime(trade.entry_time)}</td>
                          <td className="table-cell font-display text-[11px] uppercase">{titleCase(trade.strategy)}</td>
                          <td className={`table-cell font-mono text-[11px] ${trade.direction === 1 ? "text-signal-up" : "text-signal-down"}`}>
                            {trade.direction === 1 ? "LONG" : "SHORT"}
                          </td>
                          <td className={`table-cell text-right font-mono text-xs ${toneForNumber(trade.r_net)}`}>{rMultiple(trade.r_net)}</td>
                          <td className="table-cell text-right font-mono text-xs text-mist-400">{num(trade.mfe_r, 2)} / {num(trade.mae_r, 2)}</td>
                          <td className="table-cell text-[11px] text-mist-400">{titleCase(trade.exit_reason)}</td>
                          <td className="table-cell text-[11px] text-mist-500">{trade.session || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              {Object.entries(replayData.by_session).map(([session, metrics]) => (
                <div key={session} className="border border-ink-700/60 bg-ink-950/40 px-3 py-2">
                  <p className="label">{session || "unknown"}</p>
                  <p className="mt-1 font-mono text-xs">{metrics.trades} trades · <span className={toneForNumber(metrics.total_r)}>{signed(metrics.total_r)}R</span></p>
                </div>
              ))}
            </div>
          </div>
        )}
      </Panel>

      <p className="flex items-center gap-2 px-1 text-[10px] uppercase tracking-[0.14em] text-mist-600">
        <SlidersHorizontal className="h-3 w-3" aria-hidden />
        Overrides are research configuration only. No engine is promoted until every walk-forward gate passes.
      </p>
    </div>
  );
}
