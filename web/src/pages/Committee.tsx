import { useQuery } from "@tanstack/react-query";
import { BrainCircuit, MessagesSquare, RefreshCw, ShieldQuestion, TrendingDown, TrendingUp } from "lucide-react";

import { api } from "../lib/api";
import type { CommitteeSymbol } from "../lib/types";
import { age, localTime, money, num, signed } from "../lib/format";
import { Badge, EmptyState, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";

function actionTone(action: string | undefined): "ok" | "warn" | "bad" | "idle" | "gold" {
  const value = (action ?? "").toLowerCase();
  if (value.includes("fail") || value.includes("reject") || value.includes("veto")) return "bad";
  if (value.includes("order") || value.includes("fill") || value.includes("approve")) return "ok";
  if (value.includes("flat") || !value) return "idle";
  return "gold";
}

function DirectionIcon({ direction }: { direction: number | undefined }) {
  if (direction === 1) return <TrendingUp className="h-4 w-4 text-signal-up" aria-label="long bias" />;
  if (direction === -1) return <TrendingDown className="h-4 w-4 text-signal-down" aria-label="short bias" />;
  return <span className="inline-block h-2 w-2 rounded-full bg-mist-600" aria-label="flat" />;
}

function AgentVotes({ agents }: { agents: Record<string, number> | undefined }) {
  const rows = Object.entries(agents ?? {});
  if (!rows.length) {
    return <p className="text-[11px] text-mist-600">No agent votes on this board.</p>;
  }
  return (
    <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
      {rows.map(([name, vote]) => {
        const value = Number(vote);
        const label = value > 0 ? "LONG" : value < 0 ? "SHORT" : "FLAT";
        const tone = value > 0 ? "border-signal-up/35 bg-signal-up/10 text-signal-up"
          : value < 0 ? "border-signal-down/35 bg-signal-down/10 text-signal-down"
            : "border-ink-600 bg-ink-800/60 text-mist-500";
        return (
          <div key={name} className={`border px-2 py-1.5 ${tone}`}>
            <p className="font-display text-[9px] uppercase tracking-[0.1em] opacity-75">{name}</p>
            <p className="font-mono text-[10px]">{label}</p>
          </div>
        );
      })}
    </div>
  );
}

function SymbolCard({ name, row }: { name: string; row: CommitteeSymbol }) {
  const confidence = Number(row.confidence ?? 0);
  return (
    <Panel>
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-ink-700/70 px-4 py-3">
        <div>
          <div className="flex items-center gap-2">
            <h2 className="font-display text-base font-semibold tracking-wide">{name}</h2>
            <DirectionIcon direction={row.direction} />
          </div>
          <p className="mt-1 font-mono text-[10px] text-mist-500">
            {row.price ? num(row.price, 2) : "price —"} · spread {row.spread ? num(row.spread, 1) : "—"} pts
          </p>
        </div>
        <Badge tone={actionTone(row.action)}>{row.action || "no action"}</Badge>
      </div>

      <div className="grid grid-cols-3 gap-3 p-4">
        <KeyValue label="Confidence" value={confidence ? `${num(confidence * 100, 0)}%` : "—"} />
        <KeyValue label="Strength" value={signed(row.strength, 2)} />
        <KeyValue label="Regime" value={row.regime === null || row.regime === undefined ? "—" : String(row.regime)} />
      </div>

      <div className="space-y-3 border-t border-ink-700/60 p-4">
        <div>
          <p className="label mb-2">Agent votes</p>
          <AgentVotes agents={row.agents} />
        </div>

        <div className="border border-ink-700/70 bg-ink-950/40 p-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="label">LLM review</p>
            <Badge tone={row.llm ? actionTone(row.llm) : "idle"}>{row.llm || "not reviewed"}</Badge>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-mist-400">
            {row.llm_comment || "No LLM comment is present on this board. The committee never invents one."}
          </p>
        </div>

        <div className="border border-ink-700/70 bg-ink-950/40 p-2.5">
          <div className="flex items-center justify-between gap-2">
            <p className="label">Kronos</p>
            <Badge tone={row.kronos_direction === null || row.kronos_direction === undefined ? "idle" : "gold"}>
              {row.kronos_direction === 1 ? "UP" : row.kronos_direction === -1 ? "DOWN" : "NO CALL"}
            </Badge>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-mist-400">
            {row.kronos_summary || "No Kronos summary attached to this board."}
          </p>
          <div className="mt-2 flex flex-wrap gap-3 font-mono text-[10px] text-mist-500">
            <span>agreement {row.kronos_agreement !== undefined ? `${num(row.kronos_agreement * 100, 0)}%` : "—"}</span>
            <span>runs {row.kronos_runs ?? "—"}</span>
          </div>
        </div>

        <div>
          <p className="label">Why</p>
          <p className="mt-1 text-[11px] leading-relaxed text-mist-400">{row.why || "No rationale recorded."}</p>
        </div>
      </div>
    </Panel>
  );
}

export function CommitteePage() {
  const query = useQuery({ queryKey: ["committee"], queryFn: api.committee, refetchInterval: 15_000 });
  const data = query.data;
  const symbols = Object.entries(data?.symbols ?? {});
  const reviews = data?.reviews ?? [];

  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel><LoadingBlock label="Reading the committee board" /></Panel>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Committee unavailable" danger />
          <div className="p-4">
            <ErrorNote title="The committee board could not be read." detail={query.error instanceof Error ? query.error.message : String(query.error)} />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void query.refetch()}>Retry</button>
          </div>
        </Panel>
      </div>
    );
  }

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Panel className="p-4">
          <p className="label">Board</p>
          <div className="mt-2 flex items-center gap-2">
            <Badge tone={!data?.available ? "idle" : data.stale ? "warn" : "ok"}>
              {!data?.available ? "NO DATA" : data.stale ? "STALE" : "FRESH"}
            </Badge>
            <span className="font-mono text-[10px] text-mist-500">{age(data?.age_seconds)}</span>
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-mist-500">
            {data?.available ? "state/live.json written by the runner" : data?.reason ?? "No board on disk."}
          </p>
        </Panel>
        <Panel className="p-4"><KeyValue label="Mode" value={data?.mode ?? "—"} /></Panel>
        <Panel className="p-4"><KeyValue label="Board equity" value={money(data?.equity)} /></Panel>
        <Panel className="p-4">
          <KeyValue label="Board daily P/L" value={signed(data?.daily_pnl)} />
          <p className="mt-2 text-[10px] text-mist-600">Reported by the runner board, not recomputed.</p>
        </Panel>
      </div>

      {data?.warning ? (
        <div className="border border-signal-warn/35 bg-signal-warn/5 px-3 py-2.5 text-xs text-signal-warn">
          {data.warning}
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="eyebrow">Symbol reviews · {symbols.length}</p>
        <button type="button" className="btn btn-ghost" onClick={() => void query.refetch()} disabled={query.isFetching}>
          <RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? "animate-spin" : ""}`} aria-hidden />
          Refresh
        </button>
      </div>

      {symbols.length === 0 ? (
        <Panel>
          <EmptyState
            icon={<ShieldQuestion className="h-6 w-6" aria-hidden />}
            title="No committee board"
            detail={data?.reason ?? "Start the DRY-RUN runner to write state/live.json."}
          />
        </Panel>
      ) : (
        <div className="grid items-start gap-4 xl:grid-cols-2">
          {symbols.map(([name, row]) => <SymbolCard key={name} name={name} row={row} />)}
        </div>
      )}

      <Panel>
        <PanelHeader
          title="Review history"
          eyebrow="Journal · LLM decisions"
          action={<Badge tone={reviews.length ? "gold" : "idle"}>{reviews.length}</Badge>}
        />
        {data?.reviews_available === false ? (
          <div className="p-4">
            <ErrorNote title="Decision journal unavailable" detail={data.reviews_reason} />
          </div>
        ) : reviews.length === 0 ? (
          <EmptyState
            icon={<MessagesSquare className="h-6 w-6" aria-hidden />}
            title="No review history"
            detail={data?.reviews_reason ?? "The journal has no LLM review rows yet."}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2">Time</th>
                  <th className="px-3 py-2">Symbol</th>
                  <th className="px-3 py-2">Action</th>
                  <th className="px-3 py-2">LLM</th>
                  <th className="px-3 py-2">Executed</th>
                  <th className="px-3 py-2">Comment</th>
                </tr>
              </thead>
              <tbody>
                {reviews.map((review, index) => (
                  <tr key={`${review.ts}-${index}`} className="hover:bg-ink-800/40">
                    <td className="table-cell whitespace-nowrap font-mono text-[10px] text-mist-500">{localTime(review.ts)}</td>
                    <td className="table-cell font-medium">{review.symbol ?? "—"}</td>
                    <td className="table-cell">
                      <Badge tone={actionTone(review.action)}>{review.action ?? "none"}</Badge>
                    </td>
                    <td className="table-cell">
                      <Badge tone={actionTone(review.llm_action)}>{review.llm_action ?? "—"}</Badge>
                    </td>
                    <td className="table-cell font-mono text-xs">{review.executed ? "yes" : "no"}</td>
                    <td className="table-cell max-w-[420px] text-[11px] leading-relaxed text-mist-400">
                      {review.llm_comment || review.reason || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <p className="flex items-center gap-2 px-1 text-[10px] uppercase tracking-[0.14em] text-mist-600">
        <BrainCircuit className="h-3 w-3" aria-hidden />
        Committee board and journal reviews only. No reasoning is reconstructed after the fact.
      </p>
    </div>
  );
}
