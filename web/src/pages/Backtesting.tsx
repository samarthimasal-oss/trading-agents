import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Play, RefreshCw, TriangleAlert } from "lucide-react";

import { api, ApiError } from "../lib/api";
import type {
  BacktestArtifact,
  BacktestJob,
  BacktestMetrics,
  BacktestsPayload,
} from "../lib/types";
import { localTime, num, pct, rMultiple, toneForNumber } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorNote,
  LoadingBlock,
  Panel,
  PanelHeader,
} from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { useToast } from "../components/Toast";
import { useSymbols } from "../hooks/useSymbols";

const ENGINES = ["trend", "breakout", "smc", "momentum", "mean_reversion"];

function jobTone(status: string): "ok" | "warn" | "bad" | "idle" | "gold" {
  if (status === "done") return "ok";
  if (status === "running") return "gold";
  if (status === "failed") return "bad";
  if (status === "queued") return "warn";
  return "idle";
}

function paramsSummary(params: Record<string, unknown> | undefined): string {
  if (!params) return "—";
  const symbol = params.symbol ?? "—";
  const bars = params.bars ?? "—";
  const folds = params.folds ?? "—";
  const only = params.only ? ` · ${String(params.only)} only` : "";
  return `${String(symbol)} · ${String(bars)} bars · ${String(folds)} folds${only}`;
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
      <p className={`mt-1 font-mono text-base tabular-nums ${tone ?? "text-mist-100"}`}>{value}</p>
      {hint ? <p className="mt-0.5 text-[10px] text-mist-600">{hint}</p> : null}
    </div>
  );
}

function MetricsGrid({ metrics, label }: { metrics?: BacktestMetrics; label: string }) {
  if (!metrics?.available) {
    return (
      <p className="px-4 py-3 text-[11px] text-mist-600">
        {label}: {metrics?.reason ?? "not in the artifact"}.
      </p>
    );
  }
  return (
    <div>
      <p className="eyebrow px-4 pt-3">{label}</p>
      <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 xl:grid-cols-6">
        <MetricTile label="Trades" value={String(metrics.trades ?? 0)}
          hint={`${metrics.wins ?? 0}W / ${metrics.losses ?? 0}L`} />
        <MetricTile label="Total R" value={rMultiple(metrics.total_r)}
          tone={toneForNumber(metrics.total_r)} />
        <MetricTile label="Expectancy" value={rMultiple(metrics.expectancy_r)}
          tone={toneForNumber(metrics.expectancy_r)} />
        <MetricTile label="Profit factor"
          value={metrics.profit_factor === null || metrics.profit_factor === undefined
            ? "n/a"
            : num(metrics.profit_factor, 2)} />
        <MetricTile label="Win rate" value={pct(metrics.win_rate, 1)} />
        <MetricTile label="Max DD" value={rMultiple(-Math.abs(metrics.max_drawdown_r ?? 0))}
          tone="text-signal-down" />
      </div>
    </div>
  );
}

function PrimitivePairs({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data)
    .filter(([, value]) => ["number", "string", "boolean"].includes(typeof value))
    .slice(0, 12);
  if (!entries.length) return null;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1 font-mono text-[10px] sm:grid-cols-3">
      {entries.map(([key, value]) => (
        <div key={key} className="flex min-w-0 justify-between gap-2">
          <dt className="truncate text-mist-600">{key}</dt>
          <dd className="truncate text-mist-300">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function ArtifactCard({ artifact }: { artifact: BacktestArtifact }) {
  const pass = artifact.passed;
  return (
    <Panel className="flex flex-col">
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-ink-700/70 px-4 py-3">
        <div className="min-w-0">
          <p className="truncate font-mono text-[11px] text-mist-200" title={artifact.name}>
            {artifact.name}
          </p>
          <p className="mt-0.5 text-[10px] text-mist-600">{localTime(artifact.created)} local</p>
        </div>
        <Badge tone={pass === true ? "ok" : pass === false ? "bad" : "idle"}>
          {pass === true ? "WALK-FORWARD PASS" : pass === false ? "WALK-FORWARD FAIL" : "NOT MARKED"}
        </Badge>
      </div>

      {artifact.gate_counts ? (
        <div className="flex flex-wrap gap-2 border-b border-ink-700/60 px-4 py-2.5 text-[10px] uppercase tracking-[0.1em]">
          <Badge tone="ok">{artifact.gate_counts.pass} pass</Badge>
          <Badge tone={artifact.gate_counts.fail ? "bad" : "idle"}>{artifact.gate_counts.fail} fail</Badge>
          <Badge tone={artifact.gate_counts.skip ? "gold" : "idle"}>{artifact.gate_counts.skip} skipped</Badge>
        </div>
      ) : null}

      {!artifact.available ? (
        <div className="p-4">
          <ErrorNote title="Artifact unreadable" detail={artifact.reason} />
        </div>
      ) : (
        <>
          <MetricsGrid metrics={artifact.oos} label="Out-of-sample folds" />
          <MetricsGrid metrics={artifact.holdout} label="Untouched holdout (evaluated once)" />
          {artifact.data_note ? (
            <p className="px-4 pb-3 text-[10px] leading-relaxed text-mist-600">{artifact.data_note}</p>
          ) : null}
          <details className="border-t border-ink-700/60 px-4 py-2.5">
            <summary className="cursor-pointer text-[10px] uppercase tracking-[0.14em] text-mist-500 hover:text-mist-300">
              Gates, folds and run configuration
            </summary>
            <div className="mt-3 space-y-3">
              {artifact.gates?.length ? (
                <ul className="space-y-1.5">
                  {artifact.gates.map((gate, index) => (
                    <li key={`${gate.name ?? index}-${index}`} className="flex items-start justify-between gap-3">
                      <span className="font-mono text-[10px] text-mist-300">{gate.name ?? "gate"}</span>
                      <span className="flex items-center gap-2">
                        {gate.detail ? (
                          <span className="max-w-[380px] truncate text-[10px] text-mist-600" title={gate.detail}>
                            {gate.detail}
                          </span>
                        ) : null}
                        <Badge tone={gate.skipped ? "gold" : gate.passed ? "ok" : "bad"}>
                          {gate.skipped ? "SKIP" : gate.passed ? "PASS" : "FAIL"}
                        </Badge>
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
              {artifact.folds?.length ? (
                <p className="text-[10px] text-mist-600">
                  {artifact.folds.length} expanding test fold
                  {artifact.folds.length === 1 ? "" : "s"} in the artifact.
                </p>
              ) : null}
              {artifact.monte_carlo ? (
                <PrimitivePairs data={artifact.monte_carlo} />
              ) : null}
              {artifact.config ? <PrimitivePairs data={artifact.config} /> : null}
            </div>
          </details>
        </>
      )}
    </Panel>
  );
}

function JobRow({ job }: { job: BacktestJob }) {
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Badge tone={jobTone(job.status)}>{job.status.toUpperCase()}</Badge>
          <span className="truncate font-mono text-[10px] text-mist-400">{job.id}</span>
        </div>
        <span className="text-[10px] text-mist-600">{localTime(job.created)} local</span>
      </div>
      <p className="mt-1.5 font-mono text-[10px] text-mist-500">{paramsSummary(job.params)}</p>
      {job.error ? (
        <p className="mt-1 break-words text-[10px] leading-relaxed text-signal-down">{job.error}</p>
      ) : null}
    </li>
  );
}

export function BacktestingPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const symbols = useSymbols();
  const [symbol, setSymbol] = useState("XAUUSD");
  const [bars, setBars] = useState(12000);
  const [folds, setFolds] = useState(4);
  const [holdout, setHoldout] = useState(0.25);
  const [minTestBars, setMinTestBars] = useState(200);
  const [mcRuns, setMcRuns] = useState(2000);
  const [seed, setSeed] = useState(42);
  const [only, setOnly] = useState("");
  const [noSensitivity, setNoSensitivity] = useState(false);

  const listQuery = useQuery({
    queryKey: ["backtests"],
    queryFn: api.backtests,
    refetchInterval: (query) =>
      ((query.state.data as BacktestsPayload | undefined)?.jobs ?? []).some(
        (job) => job.status === "queued" || job.status === "running",
      )
        ? 4000
        : 30000,
  });

  const start = useMutation({
    mutationFn: () =>
      api.startBacktest({
        symbol,
        bars,
        folds,
        holdout,
        min_test_bars: minTestBars,
        mc_runs: mcRuns,
        seed,
        only: only || null,
        no_sensitivity: noSensitivity,
      }),
    onSuccess: (data) => {
      toast.push("success", "Walk-forward queued", `Job ${data.job.id} will run main.py walkforward.`);
      void queryClient.invalidateQueries({ queryKey: ["backtests"] });
    },
    onError: (error) => {
      toast.push("error", "Backtest start failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  const data = listQuery.data;
  const jobs = data?.jobs ?? [];
  const artifacts = data?.artifacts ?? [];
  const queue = data?.queue;
  const queueFull = queue ? queue.queued + queue.running >= queue.cap : false;

  if (listQuery.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel><LoadingBlock label="Reading backtest artifacts and jobs" /></Panel>
      </div>
    );
  }

  if (listQuery.isError || !data) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Backtesting unavailable" danger />
          <div className="p-4">
            <ErrorNote
              title="Could not read the backtest state"
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
          <p className="max-w-4xl text-[11px] leading-relaxed text-mist-500">{data.note}</p>
        </div>
        <Badge tone="gold">RESEARCH</Badge>
      </div>

      <Panel>
        <PanelHeader
          title="Start a walk-forward run"
          eyebrow="Subprocess · one at a time · artifact JSON"
          action={
            queue ? (
              <Badge tone={queueFull ? "bad" : "idle"}>
                queue {queue.queued + queue.running}/{queue.cap}
              </Badge>
            ) : null
          }
        />
        <div className="grid grid-cols-2 gap-3 px-4 py-4 sm:grid-cols-3 xl:grid-cols-6">
          <label className="block">
            <span className="label">Symbol</span>
            <select
              value={symbol}
              onChange={(event) => setSymbol(event.target.value)}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Backtest symbol"
            >
              {symbols.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="label">Bars</span>
            <input type="number" min={3000} max={30000} step={500} value={bars}
              onChange={(event) => setBars(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Backtest bars" />
          </label>
          <label className="block">
            <span className="label">Folds</span>
            <input type="number" min={1} max={8} step={1} value={folds}
              onChange={(event) => setFolds(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Walk-forward folds" />
          </label>
          <label className="block">
            <span className="label">Holdout fraction</span>
            <input type="number" min={0.05} max={0.5} step={0.05} value={holdout}
              onChange={(event) => setHoldout(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Holdout fraction" />
          </label>
          <label className="block">
            <span className="label">Monte Carlo runs</span>
            <input type="number" min={0} max={5000} step={100} value={mcRuns}
              onChange={(event) => setMcRuns(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Monte Carlo bootstrap runs" />
          </label>
          <label className="block">
            <span className="label">Seed</span>
            <input type="number" min={1} step={1} value={seed}
              onChange={(event) => setSeed(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Monte Carlo seed" />
          </label>
          <label className="block">
            <span className="label">Min test bars / fold</span>
            <input type="number" min={50} max={1000} step={10} value={minTestBars}
              onChange={(event) => setMinTestBars(Number(event.target.value))}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Minimum test bars per fold" />
          </label>
          <label className="block">
            <span className="label">Engine (optional)</span>
            <select
              value={only}
              onChange={(event) => setOnly(event.target.value)}
              className="mt-1 w-full border border-ink-600 bg-ink-950/70 px-2 py-1.5 font-mono text-xs text-mist-100"
              aria-label="Restrict to one engine"
            >
              <option value="">all five engines</option>
              {ENGINES.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          </label>
          <label className="flex items-end gap-2 pb-1.5 text-[10px] uppercase tracking-[0.12em] text-mist-500">
            <input type="checkbox" className="h-3.5 w-3.5 accent-gold-500"
              checked={noSensitivity}
              onChange={(event) => setNoSensitivity(event.target.checked)}
              aria-label="Skip the parameter sensitivity grid" />
            Skip sensitivity grid
          </label>
        </div>
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-ink-700/60 px-4 py-3">
          <p className="text-[10px] leading-relaxed text-mist-600">
            Runs <span className="font-mono">python main.py walkforward</span> against live MT5 history.
            The untouched holdout is evaluated once and is never a tuning set.
          </p>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => start.mutate()}
            disabled={start.isPending || queueFull}
          >
            {start.isPending
              ? <RefreshCw className="h-3.5 w-3.5 animate-spin" aria-hidden />
              : <Play className="h-3.5 w-3.5" aria-hidden />}
            {queueFull ? "Queue full" : "Start walk-forward"}
          </button>
        </div>
      </Panel>

      <Panel>
        <PanelHeader title="Job queue" eyebrow={`${jobs.length} recorded job${jobs.length === 1 ? "" : "s"}`} />
        {jobs.length === 0 ? (
          <EmptyState
            title="No jobs yet"
            detail="Start a walk-forward run above; the queue executes one job at a time and records every failure."
            icon={<FlaskConical className="h-6 w-6" aria-hidden />}
          />
        ) : (
          <ul className="divide-y divide-ink-700/60">
            {jobs.map((job) => <JobRow key={job.id} job={job} />)}
          </ul>
        )}
      </Panel>

      <div>
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <p className="eyebrow">Artifacts · {artifacts.length}</p>
          <span className="font-mono text-[10px] text-mist-600">docs/reports + state/backtests</span>
        </div>
        {artifacts.length === 0 ? (
          <Panel>
            <EmptyState
              title="No runs yet"
              detail="Walk-forward JSON artifacts will appear here as soon as a run writes one."
              icon={<FlaskConical className="h-6 w-6" aria-hidden />}
            />
          </Panel>
        ) : (
          <div className="grid items-start gap-3 xl:grid-cols-2">
            {artifacts.map((artifact) => (
              <ArtifactCard key={`${artifact.path}-${artifact.created ?? 0}`} artifact={artifact} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
