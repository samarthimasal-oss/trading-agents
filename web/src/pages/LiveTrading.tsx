import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Clock3,
  RefreshCw,
  Signal,
  Wallet,
} from "lucide-react";

import { api } from "../lib/api";
import type { Decision, JournalEvent, Order, Position } from "../lib/types";
import { localTime, money, num, sideLabel, signed, titleCase, toneForNumber } from "../lib/format";
import { Badge, EmptyState, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader, StatusDot } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { useLive } from "../hooks/useLiveSocket";
import { useSymbols } from "../hooks/useSymbols";
import { useToast } from "../components/Toast";

type FeedTab = "all" | "decisions" | "events";

function PositionRow({ position }: { position: Position }) {
  const side = sideLabel(position.side, position.type);
  return (
    <tr className="hover:bg-ink-800/40">
      <td className="table-cell font-mono text-xs">{position.ticket}</td>
      <td className="table-cell font-medium">{position.symbol}</td>
      <td className={`table-cell font-display text-[11px] uppercase ${side === "BUY" ? "text-signal-up" : "text-signal-down"}`}>
        {side}
      </td>
      <td className="table-cell text-right font-mono text-xs">{num(position.volume)}</td>
      <td className="table-cell text-right font-mono text-xs">{num(position.price_open)}</td>
      <td className="table-cell text-right font-mono text-xs">{num(position.price_current)}</td>
      <td className="table-cell text-right font-mono text-xs">{num(position.sl)} / {num(position.tp)}</td>
      <td className={`table-cell text-right font-mono text-xs ${toneForNumber(position.profit)}`}>
        {signed(position.profit)}
      </td>
    </tr>
  );
}

function OrderRow({ order }: { order: Order }) {
  return (
    <tr className="hover:bg-ink-800/40">
      <td className="table-cell font-mono text-xs">{order.ticket}</td>
      <td className="table-cell font-medium">{order.symbol}</td>
      <td className="table-cell font-display text-[11px] uppercase text-mist-300">{order.side ?? "—"}</td>
      <td className="table-cell text-right font-mono text-xs">{num(order.volume_current)}</td>
      <td className="table-cell text-right font-mono text-xs">{num(order.price_open)}</td>
      <td className="table-cell text-right font-mono text-xs">{num(order.sl)} / {num(order.tp)}</td>
    </tr>
  );
}

export function LiveTradingPage() {
  const live = useLive();
  const queryClient = useQueryClient();
  const toast = useToast();
  const symbols = useSymbols();
  const [symbol, setSymbol] = useState("XAUUSD");
  const [tab, setTab] = useState<FeedTab>("all");

  const ordersQuery = useQuery({ queryKey: ["orders"], queryFn: api.orders, refetchInterval: 10_000 });
  const decisionsQuery = useQuery({
    queryKey: ["decisions", 30],
    queryFn: () => api.decisions(30),
    refetchInterval: 20_000,
  });
  const eventsQuery = useQuery({
    queryKey: ["events", 30],
    queryFn: () => api.events(30),
    refetchInterval: 20_000,
  });
  const quoteQuery = useQuery({
    queryKey: ["quote", symbol],
    queryFn: () => api.marketQuote(symbol),
    refetchInterval: 5_000,
  });

  const account = live.snapshot?.account;
  const positions = (live.snapshot?.positions ?? []) as Position[];
  const orders = ordersQuery.data?.orders ?? [];
  const decisions: Decision[] = decisionsQuery.data?.decisions ?? [];
  const events: JournalEvent[] = eventsQuery.data?.events ?? [];
  const quote = quoteQuery.data;
  const floating = positions.reduce((total, position) => total + Number(position.profit ?? 0), 0);
  const runner = live.snapshot?.runner;
  const paper = (runner?.mode ?? "unknown") !== "LIVE";

  const feed = useMemo(() => {
    const rows: Array<{ kind: "decision" | "event"; item: Decision | JournalEvent }> = [];
    if (tab !== "events") for (const item of decisions) rows.push({ kind: "decision", item });
    if (tab !== "decisions") for (const item of events) rows.push({ kind: "event", item });
    return rows.sort((a, b) => Number(b.item.ts) - Number(a.item.ts)).slice(0, 40);
  }, [decisions, events, tab]);

  const refreshing = ordersQuery.isFetching || decisionsQuery.isFetching || eventsQuery.isFetching || quoteQuery.isFetching;
  const refresh = () => {
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["orders"] }),
      queryClient.invalidateQueries({ queryKey: ["decisions", 30] }),
      queryClient.invalidateQueries({ queryKey: ["events", 30] }),
      queryClient.invalidateQueries({ queryKey: ["quote", symbol] }),
    ]);
    toast.push("info", "Refreshing execution data", "Orders, journal and quote are being re-read from MT5.");
  };

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Equity</p>
            <Wallet className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className="mt-2 font-mono text-2xl tabular-nums">{money(account?.equity, account?.currency ?? "USD")}</p>
          <p className="mt-1 text-[11px] text-mist-500">Balance {money(account?.balance, account?.currency ?? "USD")}</p>
        </Panel>
        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Floating P/L</p>
            <Activity className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className={`mt-2 font-mono text-2xl tabular-nums ${toneForNumber(floating)}`}>{signed(floating)}</p>
          <p className="mt-1 text-[11px] text-mist-500">{positions.length} bot position{positions.length === 1 ? "" : "s"}</p>
        </Panel>
        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Runner</p>
            <Signal className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          <p className="mt-2 font-mono text-2xl uppercase tabular-nums">{runner?.mode ?? "stopped"}</p>
          <div className="mt-1 flex items-center gap-2">
            <StatusDot tone={paper ? "gold" : "bad"} />
            <p className="text-[11px] text-mist-500">{paper ? "DRY-RUN / PAPER only" : "LIVE reported — verify"}</p>
          </div>
        </Panel>
        <Panel className="p-4">
          <div className="flex items-start justify-between">
            <p className="label">Quote · {symbol}</p>
            <Signal className="h-4 w-4 text-mist-600" aria-hidden />
          </div>
          {quoteQuery.isError ? (
            <p className="mt-2 text-xs text-signal-down">quote unavailable</p>
          ) : (
            <>
              <div className="mt-2 flex items-baseline gap-2 font-mono tabular-nums">
                <span className="text-lg text-signal-up">{num(quote?.bid, quote?.digits ?? 2)}</span>
                <span className="text-mist-600">/</span>
                <span className="text-lg text-signal-down">{num(quote?.ask, quote?.digits ?? 2)}</span>
              </div>
              <p className="mt-1 text-[11px] text-mist-500">spread {num(quote?.spread_points, 1)} pts</p>
            </>
          )}
        </Panel>
      </div>

      <Panel>
        <PanelHeader
          title="Execution desk"
          eyebrow="MT5 · bot magic only"
          action={
            <div className="flex flex-wrap items-center gap-2">
              <label className="sr-only" htmlFor="live-symbol">Quote symbol</label>
              <select
                id="live-symbol"
                className="border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              >
                {symbols.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <button type="button" className="btn btn-ghost" onClick={refresh} disabled={refreshing}>
                <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} aria-hidden />
                Refresh
              </button>
            </div>
          }
        />
        {positions.length === 0 ? (
          <EmptyState
            icon={<ArrowDownRight className="h-6 w-6" aria-hidden />}
            title="Flat"
            detail="No open position carries the bot magic number. Manual trades are intentionally hidden."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] border-collapse">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2">Ticket</th>
                  <th className="px-3 py-2">Symbol</th>
                  <th className="px-3 py-2">Side</th>
                  <th className="px-3 py-2 text-right">Volume</th>
                  <th className="px-3 py-2 text-right">Open</th>
                  <th className="px-3 py-2 text-right">Current</th>
                  <th className="px-3 py-2 text-right">SL / TP</th>
                  <th className="px-3 py-2 text-right">P/L</th>
                </tr>
              </thead>
              <tbody>{positions.map((position) => <PositionRow key={position.ticket} position={position} />)}</tbody>
            </table>
          </div>
        )}
      </Panel>

      <div className="grid items-start gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <Panel>
          <PanelHeader
            title="Pending orders"
            eyebrow="Live broker orders"
            action={<Badge tone={orders.length ? "gold" : "idle"}>{orders.length}</Badge>}
          />
          {ordersQuery.isLoading ? (
            <LoadingBlock label="Reading orders" />
          ) : ordersQuery.isError ? (
            <div className="p-4">
              <ErrorNote title="Orders unavailable" detail={ordersQuery.error instanceof Error ? ordersQuery.error.message : String(ordersQuery.error)} />
            </div>
          ) : orders.length === 0 ? (
            <EmptyState icon={<Clock3 className="h-6 w-6" aria-hidden />} title="No pending orders" detail="The bot has no working order at the broker." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] border-collapse">
                <thead>
                  <tr className="table-head">
                    <th className="px-3 py-2">Ticket</th>
                    <th className="px-3 py-2">Symbol</th>
                    <th className="px-3 py-2">Side</th>
                    <th className="px-3 py-2 text-right">Volume</th>
                    <th className="px-3 py-2 text-right">Price</th>
                    <th className="px-3 py-2 text-right">SL / TP</th>
                  </tr>
                </thead>
                <tbody>{orders.map((order) => <OrderRow key={order.ticket} order={order} />)}</tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel>
          <PanelHeader
            title="Execution feed"
            eyebrow="Read-only journal stream"
            action={
              <div className="flex gap-1">
                {(["all", "decisions", "events"] as FeedTab[]).map((value) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setTab(value)}
                    className={`border px-2 py-1 font-display text-[9px] uppercase tracking-[0.12em] ${
                      tab === value ? "border-gold-500/60 bg-gold-500/10 text-gold-300" : "border-ink-600 text-mist-500 hover:text-mist-200"
                    }`}
                  >
                    {value}
                  </button>
                ))}
              </div>
            }
          />
          {decisionsQuery.isError || eventsQuery.isError ? (
            <div className="p-4">
              <ErrorNote title="Journal unavailable" detail="The read-only decisions/events stream could not be opened." />
            </div>
          ) : feed.length === 0 ? (
            <EmptyState
              icon={<Clock3 className="h-6 w-6" aria-hidden />}
              title="No journal activity"
              detail="Start a DRY-RUN and the runner will record decisions, fills and refusals here."
            />
          ) : (
            <ul className="max-h-[420px] divide-y divide-ink-700/50 overflow-y-auto">
              {feed.map(({ kind, item }, index) => {
                const decision = kind === "decision" ? item as Decision : null;
                const event = kind === "event" ? item as JournalEvent : null;
                const executed = Boolean(decision?.executed);
                return (
                  <li key={`${kind}-${item.ts}-${index}`} className="px-4 py-2.5">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-[10px] uppercase text-mist-600">
                        {localTime(item.ts)} · {item.symbol || "system"}
                      </span>
                      <div className="flex items-center gap-1.5">
                        {decision ? (
                          <Badge tone={executed ? "ok" : "idle"}>{decision.action || "none"}</Badge>
                        ) : (
                          <Badge tone={event?.kind === "ORDER_FILLED" ? "ok" : "idle"}>{titleCase(event?.kind)}</Badge>
                        )}
                        {decision?.llm_action ? <Badge tone="gold">LLM {decision.llm_action}</Badge> : null}
                      </div>
                    </div>
                    <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-mist-400">
                      {decision?.reason || decision?.llm_comment || event?.detail || "no detail recorded"}
                    </p>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Panel className="p-3"><KeyValue label="Free margin" value={money(account?.free_margin, account?.currency ?? "USD")} /></Panel>
        <Panel className="p-3"><KeyValue label="Margin level" value={account?.margin_level ? `${num(account.margin_level)}%` : "—"} /></Panel>
        <Panel className="p-3"><KeyValue label="Floating exposure" value={signed(floating)} tone={toneForNumber(floating)} /></Panel>
        <Panel className="p-3"><KeyValue label="Orders / positions" value={`${orders.length} / ${positions.length}`} /></Panel>
      </div>

      <p className="flex items-center gap-2 px-1 text-[10px] uppercase tracking-[0.14em] text-mist-600">
        <ArrowUpRight className="h-3 w-3" aria-hidden />
        Live account, positions and orders come from MT5; the feed is the runner journal, not a synthetic event stream.
      </p>
    </div>
  );
}
