import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowUpRight,
  CandlestickChart,
  CircleDollarSign,
  Clock3,
  PlugZap,
  ShieldAlert,
  Wallet,
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

import { api } from "../lib/api";
import type { Overview, RiskSnapshot } from "../lib/types";
import { age, localTime, money, num, signed, titleCase, toneForNumber } from "../lib/format";
import { Badge, EmptyState, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader, StatusDot } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { useLive } from "../hooks/useLiveSocket";

function isRiskSnapshot(value: Overview["risk"]): value is RiskSnapshot {
  return typeof value === "object" && value !== null && "limits" in value;
}

function bridgeTone(overview: Overview | undefined, live: ReturnType<typeof useLive>): "ok" | "warn" | "bad" | "idle" {
  const bridge = live.snapshot?.bridge ?? overview?.bridge;
  if (!bridge) return "idle";
  return bridge.listening ? "ok" : "bad";
}

export function OverviewPage() {
  const live = useLive();
  const query = useQuery({
    queryKey: ["overview"],
    queryFn: api.overview,
    refetchInterval: 20_000,
  });

  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <LoadingBlock label="Reading the account and runner journal" />
        </Panel>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Overview unavailable" eyebrow="Connection" danger />
          <div className="p-4">
            <ErrorNote
              title="The dashboard API did not answer."
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

  const data = query.data as Overview;
  const account = live.snapshot?.account ?? data.account;
  const positions = live.snapshot?.positions ?? data.positions;
  const runner = live.snapshot?.runner ?? data.runner;
  const risk = isRiskSnapshot(data.risk) ? data.risk : null;
  const bridge = live.snapshot?.bridge ?? data.bridge;
  const equity = data.equity ?? [];
  const committee = data.committee;
  const paper = (runner?.mode ?? "unknown") !== "LIVE";

  const floating = positions.reduce((total, position) => total + Number(position.profit ?? 0), 0);

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Equity</p>
            <Wallet className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className="mt-2 font-mono text-2xl tabular-nums tracking-tight">
            {money(account?.equity, account?.currency ?? "USD")}
          </p>
          <p className="mt-1 text-[11px] text-mist-500">
            Balance {money(account?.balance, account?.currency ?? "USD")}
          </p>
        </Panel>

        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Floating P/L</p>
            <CircleDollarSign className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className={`mt-2 font-mono text-2xl tabular-nums tracking-tight ${toneForNumber(floating)}`}>
            {signed(floating)}
          </p>
          <p className="mt-1 text-[11px] text-mist-500">
            {positions.length} open position{positions.length === 1 ? "" : "s"}
          </p>
        </Panel>

        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Runner</p>
            <Activity className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className="mt-2 font-mono text-2xl uppercase tabular-nums tracking-tight">
            {runner?.mode ?? "stopped"}
          </p>
          <div className="mt-1 flex items-center gap-2">
            <StatusDot tone={paper ? "gold" : "bad"} />
            <p className="text-[11px] text-mist-500">
              {paper ? "DRY-RUN / PAPER only" : "LIVE reported — verify"}
            </p>
          </div>
        </Panel>

        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">EA bridge</p>
            <PlugZap className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className="mt-2 font-mono text-2xl tabular-nums tracking-tight">
            {bridge?.listening ? "ONLINE" : "DOWN"}
          </p>
          <div className="mt-1 flex items-center gap-2">
            <StatusDot tone={bridgeTone(data, live)} />
            <p className="truncate text-[11px] text-mist-500">
              {bridge?.host ?? "127.0.0.1"}:{bridge?.port ?? 8765}
            </p>
          </div>
        </Panel>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[1.35fr_0.65fr]">
        <Panel>
          <PanelHeader
            title="Equity curve"
            eyebrow="Journal · equity_log"
            action={
              <span className="text-[10px] uppercase tracking-[0.14em] text-mist-600">
                {equity.length} point{equity.length === 1 ? "" : "s"}
              </span>
            }
          />
          {equity.length < 2 ? (
            <EmptyState
              icon={<CandlestickChart className="h-6 w-6" aria-hidden />}
              title="No equity history yet"
              detail="The runner writes an equity point each poll. Start a DRY-RUN from the Risk Centre once the EA is connected."
            />
          ) : (
            <div className="h-[260px] px-2 py-3">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equity} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#d9ae62" stopOpacity={0.32} />
                      <stop offset="100%" stopColor="#d9ae62" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid stroke="#1f2934" strokeDasharray="2 4" vertical={false} />
                  <XAxis
                    dataKey="ts"
                    tickFormatter={(value: number) => localTime(value).slice(-8)}
                    stroke="#647181"
                    tick={{ fontSize: 10, fontFamily: "Cascadia Mono, monospace" }}
                    tickLine={false}
                    axisLine={{ stroke: "#1f2934" }}
                    minTickGap={40}
                  />
                  <YAxis
                    domain={["auto", "auto"]}
                    tickFormatter={(value: number) => num(value, 0)}
                    stroke="#647181"
                    tick={{ fontSize: 10, fontFamily: "Cascadia Mono, monospace" }}
                    tickLine={false}
                    axisLine={false}
                    width={62}
                  />
                  <Tooltip
                    contentStyle={{
                      background: "#0b0e12",
                      border: "1px solid #2a3644",
                      fontFamily: "Cascadia Mono, monospace",
                      fontSize: 11,
                    }}
                    labelFormatter={(value) => localTime(Number(value))}
                    formatter={(value: number) => [money(value), "Equity"]}
                  />
                  <Area
                    type="monotone"
                    dataKey="equity"
                    stroke="#d9ae62"
                    strokeWidth={1.6}
                    fill="url(#equityFill)"
                    dot={false}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </Panel>

        <div className="space-y-4">
          <Panel>
            <PanelHeader
              title="Risk governor"
              eyebrow="Daily loss · drawdown"
              action={
                <Link to="/risk" className="link font-display text-[10px] uppercase tracking-[0.14em]">
                  Open
                </Link>
              }
            />
            <div className="space-y-3 p-4">
              {!risk ? (
                <ErrorNote title="Risk snapshot unavailable" detail={String((data.risk as { reason?: string })?.reason ?? "no data")} />
              ) : (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <KeyValue label="Daily P/L" value={risk.daily_pnl === null || risk.daily_pnl === undefined ? "—" : signed(risk.daily_pnl)} tone={toneForNumber(risk.daily_pnl)} />
                    <KeyValue label="Loss limit" value={money(risk.daily_loss_limit_usd)} />
                    <KeyValue label="Peak equity · journal" value={money(risk.peak_equity)} />
                    <KeyValue label="Drawdown" value={risk.drawdown_from_peak_pct === null || risk.drawdown_from_peak_pct === undefined ? "—" : `${num(risk.drawdown_from_peak_pct)}%`} />
                  </div>
                  <div className="hairline pt-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="label">Guardian</span>
                      <Badge tone={risk.guardian?.halted_day ? "bad" : risk.guardian?.available ? "ok" : "idle"}>
                        {risk.guardian?.halted_day ? "HALTED" : risk.guardian?.available ? "ARMED" : "NO STATE"}
                      </Badge>
                    </div>
                    <p className="mt-2 text-[11px] leading-relaxed text-mist-500">
                      {risk.guardian?.halted_day
                        ? `Halted ${risk.guardian.halted_day}: ${risk.guardian.halt_reason || "no reason recorded"}`
                        : risk.guardian?.available
                          ? `Day ${risk.guardian.day ?? "—"} · start equity ${money(risk.guardian.day_start_equity)}`
                          : "The guardian has not written state yet."}
                    </p>
                  </div>
                </>
              )}
            </div>
          </Panel>

          <Panel>
            <PanelHeader
              title="AI committee"
              eyebrow="state/live.json"
              action={
                <Link to="/committee" className="link font-display text-[10px] uppercase tracking-[0.14em]">
                  Open
                </Link>
              }
            />
            <div className="space-y-3 p-4">
              <div className="flex items-center justify-between gap-2">
                <span className="label">Board</span>
                <Badge tone={!committee?.available ? "idle" : committee.stale ? "warn" : "ok"}>
                  {!committee?.available ? "NO DATA" : committee.stale ? "STALE" : "FRESH"}
                </Badge>
              </div>
              <p className="text-[11px] leading-relaxed text-mist-500">
                {committee?.available
                  ? `${Object.keys(committee.symbols ?? {}).length} symbol(s) reviewed · age ${age(committee.age_seconds)}`
                  : committee?.reason ?? "The legacy runner has not written a committee board."}
              </p>
              {committee?.warning ? (
                <p className="border border-signal-warn/30 bg-signal-warn/5 px-2 py-1.5 text-[10px] text-signal-warn">
                  {committee.warning}
                </p>
              ) : null}
              <div className="hairline pt-3">
                <KeyValue
                  label="Journal"
                  value={`${data.journal?.decisions ?? 0} decisions · ${data.journal?.filled_orders ?? 0} fills`}
                />
              </div>
            </div>
          </Panel>
        </div>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-[1.4fr_1fr]">
        <Panel>
          <PanelHeader title="Open positions" eyebrow="MT5 · bot magic" />
          {positions.length === 0 ? (
            <EmptyState
              icon={<ShieldAlert className="h-6 w-6" aria-hidden />}
              title="Flat"
              detail="No position carries the bot magic number. Manual trades are intentionally hidden from this view."
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] border-collapse">
                <thead>
                  <tr className="table-head">
                    <th className="px-3 py-2">Ticket</th>
                    <th className="px-3 py-2">Symbol</th>
                    <th className="px-3 py-2">Side</th>
                    <th className="px-3 py-2 text-right">Volume</th>
                    <th className="px-3 py-2 text-right">Open</th>
                    <th className="px-3 py-2 text-right">SL / TP</th>
                    <th className="px-3 py-2 text-right">P/L</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((position) => (
                    <tr key={position.ticket} className="hover:bg-ink-800/40">
                      <td className="table-cell font-mono text-xs">{position.ticket}</td>
                      <td className="table-cell font-medium">{position.symbol}</td>
                      <td className="table-cell">
                        <span className={(position.side ?? "").toLowerCase() === "buy" ? "text-signal-up" : "text-signal-down"}>
                          {(position.side ?? (position.type === 0 ? "BUY" : "SELL")).toUpperCase()}
                        </span>
                      </td>
                      <td className="table-cell text-right font-mono text-xs">{num(position.volume)}</td>
                      <td className="table-cell text-right font-mono text-xs">{num(position.price_open)}</td>
                      <td className="table-cell text-right font-mono text-xs">
                        {num(position.sl)} / {num(position.tp)}
                      </td>
                      <td className={`table-cell text-right font-mono text-xs ${toneForNumber(position.profit)}`}>
                        {signed(position.profit)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel>
          <PanelHeader title="Activity" eyebrow="Latest decisions + events" />
          {data.decisions.length === 0 && data.events.length === 0 ? (
            <EmptyState
              icon={<Clock3 className="h-6 w-6" aria-hidden />}
              title="No journal activity"
              detail="Start a DRY-RUN and the runner will record decisions, fills and refusals here."
            />
          ) : (
            <ul className="max-h-[330px] divide-y divide-ink-700/50 overflow-y-auto">
              {data.decisions.slice(0, 6).map((decision, index) => (
                <li key={`decision-${decision.ts}-${index}`} className="px-4 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[10px] uppercase text-mist-600">
                      {localTime(decision.ts)} · {decision.symbol}
                    </span>
                    <Badge tone={decision.executed ? "ok" : "idle"}>{decision.action}</Badge>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-mist-400">
                    {decision.reason || "no reason recorded"}
                  </p>
                </li>
              ))}
              {data.events.slice(0, 6).map((event, index) => (
                <li key={`event-${event.ts}-${index}`} className="px-4 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[10px] uppercase text-mist-600">
                      {localTime(event.ts)} · {event.symbol || "system"}
                    </span>
                    <span className="badge badge-idle">{titleCase(event.kind)}</span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[11px] leading-relaxed text-mist-400">
                    {event.detail || "no detail"}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-[10px] uppercase tracking-[0.14em] text-mist-600">
        <span>
          Snapshot {localTime(data.generated_at)} · data from MT5 + read-only journal
        </span>
        <span className="flex items-center gap-1.5">
          <ArrowUpRight className="h-3 w-3" aria-hidden />
          refresh every 20s · WebSocket live
        </span>
      </div>
    </div>
  );
}
