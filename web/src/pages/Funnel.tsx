import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Filter, RefreshCw, TriangleAlert } from "lucide-react";

import { api } from "../lib/api";
import type { FunnelStage } from "../lib/types";
import {
  EM_DASH,
  localTime,
  money,
  num,
  pct,
  titleCase,
  toneForNumber,
} from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorNote,
  LoadingBlock,
  Panel,
  PanelHeader,
} from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";

const WINDOWS = [
  { hours: 24, label: "24h" },
  { hours: 72, label: "3d" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
];

function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="border border-ink-700/70 bg-ink-950/40 px-3 py-2.5">
      <p className="label">{label}</p>
      <p className={`mt-1 font-mono text-lg tabular-nums ${tone ?? "text-mist-100"}`}>{value}</p>
      {hint ? <p className="mt-0.5 text-[10px] text-mist-600">{hint}</p> : null}
    </div>
  );
}

function StageCard({ stage, index, journalAvailable }: {
  stage: FunnelStage;
  index: number;
  journalAvailable: boolean;
}) {
  const unavailable = !journalAvailable || stage.available === false;
  const closed = stage.key === "closed";
  const ratio = stage.reached > 0 ? Math.min(100, (stage.passed / stage.reached) * 100) : 0;
  return (
    <article
      aria-label={`${stage.label} stage`}
      className={`border px-3 py-2.5 ${
        unavailable
          ? "border-signal-down/30 bg-signal-down/5"
          : "border-ink-700/70 bg-ink-950/40"
      }`}
    >
      <p className="label">
        {String(index + 1).padStart(2, "0")} · {stage.label}
      </p>
      <p className="mt-1 font-mono text-2xl leading-none tabular-nums text-mist-100">
        {unavailable ? EM_DASH : num(stage.reached, 0)}
      </p>
      <p className="mt-1 text-[10px] text-mist-600">
        {unavailable
          ? stage.reason ?? "not available"
          : closed
            ? `${num(stage.passed, 0)} wins · ${num(stage.rejected, 0)} losses`
            : `${num(stage.passed, 0)} passed`}
      </p>
      {!unavailable ? (
        <>
          <div className="mt-2 h-1 bg-ink-700" aria-hidden>
            <div className="h-1 bg-gold-400" style={{ width: `${ratio}%` }} />
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {closed ? (
              <>
                <Badge tone="gold">
                  win rate {stage.win_rate === null || stage.win_rate === undefined
                    ? EM_DASH
                    : pct(stage.win_rate, 1)}
                </Badge>
                <Badge tone={Number(stage.net) >= 0 ? "ok" : "bad"}>
                  net {money(stage.net)}
                </Badge>
              </>
            ) : (
              <>
                <Badge tone="ok">{num(stage.passed, 0)} pass</Badge>
                {stage.rejected > 0 ? <Badge tone="bad">{num(stage.rejected, 0)} stop</Badge> : null}
                {stage.skipped > 0 ? <Badge tone="warn">{num(stage.skipped, 0)} skip</Badge> : null}
              </>
            )}
          </div>
        </>
      ) : null}
    </article>
  );
}

export function FunnelPage() {
  const [hours, setHours] = useState(24);
  const query = useQuery({
    queryKey: ["funnel", hours],
    queryFn: () => api.funnel(hours),
  });

  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <LoadingBlock label="Reading recorded stage evaluations" />
        </Panel>
      </div>
    );
  }

  const payload = query.data;
  if (query.isError || !payload) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Signal funnel unavailable" danger />
          <div className="p-4">
            <ErrorNote
              title="Could not read the funnel"
              detail={query.error instanceof Error ? query.error.message : String(query.error)}
            />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void query.refetch()}>
              Retry
            </button>
          </div>
        </Panel>
      </div>
    );
  }

  const stages = payload.stages ?? [];
  const rejections = payload.rejections ?? [];
  const bySymbol = payload.by_symbol ?? [];
  const orders = payload.orders;
  const rejectionTotal = rejections.reduce((total, row) => total + row.count, 0);
  const labels = Object.fromEntries(stages.map((stage) => [stage.key, stage.label]));
  const noRows = payload.available && payload.evaluations === 0;

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="flex flex-wrap items-start justify-between gap-3 border border-gold-500/25 bg-gold-500/5 px-4 py-3">
        <div className="flex items-start gap-2.5">
          <Filter className="mt-0.5 h-4 w-4 shrink-0 text-gold-300" aria-hidden />
          <div>
            <p className="font-display text-[11px] font-semibold uppercase tracking-[0.16em] text-gold-300">
              Every count is recorded data
            </p>
            <p className="mt-0.5 max-w-4xl text-[11px] leading-relaxed text-mist-500">
              {payload.note}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge tone={payload.available ? "gold" : "bad"}>
            {payload.available ? "JOURNAL ONLINE" : "JOURNAL OFFLINE"}
          </Badge>
          <Badge tone="idle">{payload.cached ? "cached" : "fresh"}</Badge>
          {payload.latest_ts ? (
            <Badge tone="idle">latest {localTime(payload.latest_ts)}</Badge>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-1" role="group" aria-label="Funnel window">
          {WINDOWS.map((option) => (
            <button
              key={option.hours}
              type="button"
              aria-pressed={hours === option.hours}
              onClick={() => setHours(option.hours)}
              className={`border px-2.5 py-1 font-mono text-[10px] ${
                hours === option.hours
                  ? "border-gold-500/60 bg-gold-500/10 text-gold-300"
                  : "border-ink-600 text-mist-500 hover:text-mist-200"
              }`}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-mist-600">
            {num(payload.evaluations, 0)} stage rows · since {localTime(payload.since)}
          </span>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? "animate-spin" : ""}`} aria-hidden />
            Refresh
          </button>
        </div>
      </div>

      {!payload.available ? (
        <ErrorNote
          title="The runner journal is not readable"
          detail="Start the runner (or check state/agents.sqlite) so stage evaluations can be recorded."
        />
      ) : null}
      {noRows ? (
        <div className="flex items-start gap-2 border border-signal-warn/30 bg-signal-warn/5 px-3 py-2.5">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-signal-warn" aria-hidden />
          <p className="text-[11px] leading-relaxed text-mist-500">
            {payload.note} Nothing is estimated in the meantime; the counts stay at zero until the
            runner evaluates a closed bar.
          </p>
        </div>
      ) : null}

      <Panel>
        <PanelHeader
          title="Conversion by stage"
          eyebrow="Signals → agents → Kronos → Gemini → risk → execution → closed"
        />
        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">
          {stages.map((stage, index) => (
            <StageCard key={stage.key} stage={stage} index={index}
              journalAvailable={payload.available} />
          ))}
        </div>
      </Panel>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-5">
        <Tile label="Orders filled" value={num(orders.filled, 0)} hint="broker-confirmed deals" />
        <Tile label="Orders rejected" value={num(orders.rejected, 0)} hint="journal ORDER_REJECTED" />
        <Tile label="Bot closes" value={num(orders.closed, 0)} hint="journal ORDER_CLOSED" />
        <Tile label="Partial closes" value={num(orders.partial_closes, 0)} hint="1R profit taking" />
        <Tile label="SL modifications" value={num(orders.sl_modified, 0)} hint="breakeven / trailing" />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Panel>
          <PanelHeader
            title="Where candidates stopped"
            eyebrow={`${num(rejectionTotal, 0)} recorded rejection${rejectionTotal === 1 ? "" : "s"}`}
          />
          {rejections.length === 0 ? (
            <EmptyState
              title="No rejections in this window"
              detail="The table fills as soon as a gate records a reject, veto or loss outcome."
              icon={<Filter className="h-6 w-6" aria-hidden />}
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[420px] text-left">
                <thead>
                  <tr className="border-b border-ink-700 text-[9px] uppercase tracking-[0.14em] text-mist-600">
                    <th scope="col" className="px-3 py-2 font-medium">Stage</th>
                    <th scope="col" className="px-3 py-2 font-medium">Reason</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Count</th>
                  </tr>
                </thead>
                <tbody>
                  {rejections.map((row, index) => (
                    <tr key={`${row.stage}-${row.reason}-${index}`} className="border-b border-ink-700/50 align-top">
                      <td className="whitespace-nowrap px-3 py-2 text-[10px] text-mist-400">
                        {labels[row.stage] ?? titleCase(row.stage)}
                      </td>
                      <td className="px-3 py-2 text-[11px] leading-relaxed text-mist-300">{row.reason}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] tabular-nums text-mist-200">
                        {num(row.count, 0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel>
          <PanelHeader title="By symbol" eyebrow="same window, real rows only" />
          {bySymbol.length === 0 ? (
            <EmptyState
              title="No symbol activity recorded"
              detail="Evaluations are written per symbol per closed bar."
              icon={<Filter className="h-6 w-6" aria-hidden />}
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[420px] text-left">
                <thead>
                  <tr className="border-b border-ink-700 text-[9px] uppercase tracking-[0.14em] text-mist-600">
                    <th scope="col" className="px-3 py-2 font-medium">Symbol</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Signals</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Executed</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Rejections</th>
                    <th scope="col" className="px-3 py-2 text-right font-medium">Closed</th>
                  </tr>
                </thead>
                <tbody>
                  {bySymbol.map((row) => (
                    <tr key={row.symbol} className="border-b border-ink-700/50">
                      <td className="px-3 py-2 font-mono text-[11px] text-mist-200">{row.symbol}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] tabular-nums text-mist-300">{num(row.signals, 0)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] tabular-nums text-mist-300">{num(row.executed, 0)}</td>
                      <td className="px-3 py-2 text-right font-mono text-[11px] tabular-nums text-mist-300">{num(row.rejected, 0)}</td>
                      <td className={`px-3 py-2 text-right font-mono text-[11px] tabular-nums ${toneForNumber(row.closed)}`}>{num(row.closed, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      <Panel>
        <PanelHeader title="How to read this" eyebrow="No inferred metrics" />
        <div className="space-y-1.5 px-4 py-3 text-[11px] leading-relaxed text-mist-500">
          <p>
            <span className="text-mist-300">Signals</span> counts closed bars where the quant consensus
            produced a direction. <span className="text-mist-300">Agents</span> counts bars where the five
            quant strategies plus Kronos returned votes; <span className="text-mist-300">Kronos</span> and{" "}
            <span className="text-mist-300">Gemini</span> show whether their input was available and whether
            an advisory veto was applied.
          </p>
          <p>
            <span className="text-mist-300">Risk</span> is the final gate/sizing decision and{" "}
            <span className="text-mist-300">execution</span> is a broker-confirmed fill. Closed trades come
            from the terminal&apos;s deal history and are not attributed to a stage row; an empty journal
            therefore shows zeros, never a made-up conversion rate.
          </p>
          <p className="text-mist-600">
            A bar counts once per stage. “Skip” means the stage was not applicable (Kronos disabled or no
            forecast); “stop” means the candidate was rejected there.
          </p>
        </div>
      </Panel>
    </div>
  );
}
