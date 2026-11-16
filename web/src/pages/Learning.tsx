import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, TriangleAlert } from "lucide-react";

import { api } from "../lib/api";
import type { LearningPayload, LearningTrade } from "../lib/types";
import {
  EM_DASH,
  localTime,
  money,
  num,
  rMultiple,
  sideLabel,
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

function MetricsTile({
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

function priceAt(value: number, digits?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return EM_DASH;
  return Number(value).toFixed(Math.max(0, Math.min(Number(digits ?? 2), 8)));
}

function TradeRow({ trade }: { trade: LearningTrade }) {
  return (
    <tr className="border-b border-ink-700/50 align-top">
      <td className="px-3 py-2 font-mono text-[10px] text-mist-400">
        {localTime(trade.open_time)}
        <span className="block text-mist-600">→ {localTime(trade.close_time)}</span>
      </td>
      <td className="px-3 py-2 font-mono text-[11px] text-mist-200">{trade.symbol}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-mist-200">{sideLabel(trade.side)}</td>
      <td className="px-3 py-2 font-mono text-[11px] tabular-nums text-mist-300">{num(trade.volume, 2)}</td>
      <td className="px-3 py-2 font-mono text-[11px] tabular-nums text-mist-300">{priceAt(trade.entry_price, trade.digits)}</td>
      <td className="px-3 py-2 font-mono text-[11px] tabular-nums text-mist-300">{priceAt(trade.exit_price, trade.digits)}</td>
      <td className={`px-3 py-2 font-mono text-[11px] tabular-nums ${toneForNumber(trade.net)}`}>
        {money(trade.net)}
      </td>
      <td className={`px-3 py-2 font-mono text-[11px] tabular-nums ${toneForNumber(trade.realized_r)}`}>
        {trade.realized_r === null ? EM_DASH : rMultiple(trade.realized_r)}
        {trade.realized_r === null ? (
          <span className="block text-[9px] text-mist-600">no entry SL</span>
        ) : null}
      </td>
      <td className="px-3 py-2 text-[10px] text-mist-400">{titleCase(trade.session)}</td>
      <td className="px-3 py-2">
        <Badge tone={trade.is_bot ? "gold" : "idle"}>
          {trade.is_bot ? "BOT" : `manual ${trade.magic}`}
        </Badge>
      </td>
    </tr>
  );
}

function Unavailable({ payload }: { payload: LearningPayload }) {
  return (
    <Panel>
      <PanelHeader title="Learning Centre unavailable" danger />
      <div className="space-y-3 p-4">
        <ErrorNote title="Closed-trade data is not available" detail={payload.reason} />
        <div>
          <p className="label mb-1.5">Fields that cannot be produced yet</p>
          <ul className="flex flex-wrap gap-1.5">
            {(payload.missing_fields ?? []).map((field) => (
              <li key={field}>
                <Badge tone="warn">{titleCase(field)}</Badge>
              </li>
            ))}
          </ul>
        </div>
        {payload.next_step ? (
          <p className="text-[11px] leading-relaxed text-mist-600">Next step: {payload.next_step}</p>
        ) : null}
      </div>
    </Panel>
  );
}

export function LearningPage() {
  const summaryQuery = useQuery({ queryKey: ["learning-summary"], queryFn: api.learningSummary });
  const tradesQuery = useQuery({ queryKey: ["learning-trades"], queryFn: () => api.learningTrades(200) });

  if (summaryQuery.isLoading || tradesQuery.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel><LoadingBlock label="Reading closed-trade history" /></Panel>
      </div>
    );
  }

  const summary = summaryQuery.data;
  const trades = tradesQuery.data;

  if (summaryQuery.isError || !summary) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Learning Centre unavailable" danger />
          <div className="p-4">
            <ErrorNote
              title="Could not read the learning summary"
              detail={summaryQuery.error instanceof Error ? summaryQuery.error.message : String(summaryQuery.error)}
            />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void summaryQuery.refetch()}>
              Retry
            </button>
          </div>
        </Panel>
      </div>
    );
  }

  if (!summary.available) {
    return (
      <div className="space-y-4 animate-rise">
        <SafetyBanner />
        <Unavailable payload={summary} />
      </div>
    );
  }

  const metrics = summary.metrics;
  const rows = trades?.trades ?? [];

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="flex flex-wrap items-start justify-between gap-3 border border-gold-500/25 bg-gold-500/5 px-4 py-3">
        <div className="flex items-start gap-2.5">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-gold-300" aria-hidden />
          <p className="max-w-4xl text-[11px] leading-relaxed text-mist-500">
            {summary.attribution_note ??
              "Rows come from the terminal's deal history; strategy and committee attribution needs the Phase 6 closed-trade store."}
          </p>
        </div>
        <Badge tone="gold">{summary.data_source ?? "MT5 deal history"}</Badge>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <MetricsTile label="Closed trades" value={String(metrics?.closed_trades ?? 0)}
          hint={`${metrics?.bot_trades ?? 0} bot · ${metrics?.non_bot_trades ?? 0} other magic`} />
        <MetricsTile label="Win rate"
          value={metrics?.win_rate === null || metrics?.win_rate === undefined
            ? EM_DASH
            : `${num(metrics.win_rate, 1)}%`}
          hint={`${metrics?.wins ?? 0}W / ${metrics?.losses ?? 0}L`} />
        <MetricsTile label="Total net P/L" value={money(metrics?.total_net ?? null)}
          tone={toneForNumber(metrics?.total_net)} />
        <MetricsTile label="Average R"
          value={metrics?.avg_r === null || metrics?.avg_r === undefined
            ? EM_DASH
            : rMultiple(metrics.avg_r)}
          hint={`${metrics?.r_known ?? 0} of ${metrics?.closed_trades ?? 0} trades have a known R`} />
        <MetricsTile label="Window" value={`${summary.window_days ?? 180} d`}
          hint="deal history looked back" />
        <MetricsTile label="Journal" value={summary.journal_available ? "online" : "offline"}
          tone={summary.journal_available ? "text-mist-100" : "text-signal-warn"}
          hint="runner decision journal" />
      </div>

      <Panel>
        <PanelHeader
          title="Closed trades"
          eyebrow={`${rows.length} row${rows.length === 1 ? "" : "s"} · most recent first`}
          action={
            summary.note ? <span className="text-[10px] text-mist-600">{summary.note}</span> : null
          }
        />
        {rows.length === 0 ? (
          <EmptyState
            title="No closed trades yet"
            detail={summary.reason ??
              "The page lists closed trades as soon as the account books one; nothing is synthesised in the meantime."}
            icon={<BrainCircuit className="h-6 w-6" aria-hidden />}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px] text-left">
              <thead>
                <tr className="border-b border-ink-700 text-[9px] uppercase tracking-[0.14em] text-mist-600">
                  <th scope="col" className="px-3 py-2 font-medium">Opened → closed</th>
                  <th scope="col" className="px-3 py-2 font-medium">Symbol</th>
                  <th scope="col" className="px-3 py-2 font-medium">Side</th>
                  <th scope="col" className="px-3 py-2 font-medium">Volume</th>
                  <th scope="col" className="px-3 py-2 font-medium">Entry</th>
                  <th scope="col" className="px-3 py-2 font-medium">Exit</th>
                  <th scope="col" className="px-3 py-2 font-medium">Net P/L</th>
                  <th scope="col" className="px-3 py-2 font-medium">Realized R</th>
                  <th scope="col" className="px-3 py-2 font-medium">Session</th>
                  <th scope="col" className="px-3 py-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((trade) => (
                  <TradeRow key={trade.position_id} trade={trade} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel>
        <PanelHeader title="Not recorded yet" eyebrow="Honest gaps" />
        <div className="px-4 py-3">
          <p className="text-[11px] leading-relaxed text-mist-500">
            The MT5 deal record cannot answer these; they are shown as <span className="font-mono">—</span> rather
            than inferred:
          </p>
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {(summary.missing_fields ?? []).map((field) => (
              <li key={field}>
                <Badge tone="idle">{titleCase(field)}</Badge>
              </li>
            ))}
          </ul>
          {metrics && Object.keys(metrics.sessions).length ? (
            <p className="mt-3 text-[10px] leading-relaxed text-mist-600">
              Sessions seen:{" "}
              {Object.entries(metrics.sessions)
                .map(([session, count]) => `${session} ×${count}`)
                .join(" · ")}
            </p>
          ) : null}
        </div>
      </Panel>
    </div>
  );
}
