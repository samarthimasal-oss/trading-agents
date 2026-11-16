import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  CalendarClock,
  Compass,
  Gauge,
  Newspaper,
  Radio,
  RefreshCw,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { api } from "../lib/api";
import type { MarketOverview } from "../lib/types";
import { age, localTime, num, pct, signed, titleCase } from "../lib/format";
import { Badge, EmptyState, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { CandleChart } from "../components/CandleChart";
import { useSymbols } from "../hooks/useSymbols";

const TIMEFRAMES = ["M15", "M30", "H1", "H4", "D1"];

function directionLabel(value: number | null | undefined): string {
  if (value === 1) return "UP";
  if (value === -1) return "DOWN";
  return "NO CALL";
}

function rsiTone(value: number | undefined): string {
  if (value === undefined) return "text-mist-300";
  if (value >= 70) return "text-signal-down";
  if (value <= 30) return "text-signal-up";
  return "text-mist-100";
}

/**
 * Kronos cache values are absolute price deltas, not fractions.  Show the
 * price distance and, when the cached base close is known, the percentage it
 * represents.  Never relabel one as the other.
 */
function kronosValue(value: number | null | undefined, base: number | null | undefined, digits: number): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const price = signed(value, digits);
  if (!base || !Number.isFinite(Number(base))) return price;
  return `${price} (${pct((Number(value) / Number(base)) * 100, 2)})`;
}

export function MarketIntelligencePage() {
  const symbols = useSymbols();
  const [symbol, setSymbol] = useState("XAUUSD");
  const [timeframe, setTimeframe] = useState("M15");

  const query = useQuery<MarketOverview>({
    queryKey: ["market-overview", symbol, timeframe],
    queryFn: () => api.marketOverview(symbol, timeframe),
    refetchInterval: 15_000,
  });

  const data = query.data;
  const quote = data?.quote;
  const features = data?.features;
  const kronos = data?.kronos;
  const news = data?.news;
  const digits = quote?.digits ?? 2;
  const lastCandle = data?.candles?.[data.candles.length - 1];
  const rawForecast = kronos?.available ? (kronos.forecast_candles ?? []) : [];
  // A forecast drawn against a detached base would imply continuity that does
  // not exist.  Keep the numbers visible but omit the path when it is stale
  // or starts away from the current price.
  const forecastAligned = Boolean(
    rawForecast.length
    && lastCandle
    && Number.isFinite(Number(rawForecast[0]?.[1]))
    && Math.abs(Number(rawForecast[0][1]) - lastCandle.close) <= Math.abs(lastCandle.close) * 0.002,
  );
  const forecast = kronos?.stale || !forecastAligned ? [] : rawForecast;
  const forecastNote = rawForecast.length === 0
    ? null
    : kronos?.stale
      ? "Forecast path not drawn: the cache is stale."
      : !forecastAligned
        ? "Forecast path not drawn: its first bar is detached from the current price."
        : null;
  const newsEvents = news?.events ?? [];
  const upcoming = news?.upcoming ?? [];

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="flex flex-wrap items-center justify-between gap-3 border border-ink-700 bg-ink-900/60 px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Compass className="h-4 w-4 text-gold-400" aria-hidden />
          <label className="sr-only" htmlFor="market-symbol">Symbol</label>
          <select
            id="market-symbol"
            className="border border-ink-600 bg-ink-950/80 px-2 py-1.5 font-mono text-xs text-mist-200"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
          >
            {symbols.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
          <div className="flex gap-1" role="group" aria-label="Timeframe">
            {TIMEFRAMES.map((value) => (
              <button
                key={value}
                type="button"
                aria-pressed={timeframe === value}
                onClick={() => setTimeframe(value)}
                className={`border px-2 py-1 font-mono text-[10px] ${
                  timeframe === value
                    ? "border-gold-500/60 bg-gold-500/10 text-gold-300"
                    : "border-ink-600 text-mist-500 hover:text-mist-200"
                }`}
              >
                {value}
              </button>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge tone={data?.tradable ? "ok" : "warn"}>{data ? (data.tradable ? "TRADABLE" : "BLOCKED") : "NO DATA"}</Badge>
          <button type="button" className="btn btn-ghost" onClick={() => void query.refetch()} disabled={query.isFetching}>
            <RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? "animate-spin" : ""}`} aria-hidden />
            Refresh
          </button>
        </div>
      </div>

      {query.isError ? (
        <Panel>
          <PanelHeader title="Market snapshot unavailable" danger />
          <div className="p-4">
            <ErrorNote
              title="Could not read the market snapshot"
              detail={query.error instanceof Error ? query.error.message : String(query.error)}
            />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void query.refetch()}>Retry</button>
          </div>
        </Panel>
      ) : !data ? (
        <Panel><LoadingBlock label="Reading quote, candles and features" /></Panel>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Panel className="p-4">
              <div className="flex items-start justify-between">
                <p className="label">Bid / Ask</p>
                <Radio className="h-4 w-4 text-mist-600" aria-hidden />
              </div>
              <div className="mt-2 flex items-baseline gap-1.5 font-mono tabular-nums">
                <span className="text-lg text-signal-up">{num(quote?.bid, digits)}</span>
                <span className="text-mist-600">/</span>
                <span className="text-lg text-signal-down">{num(quote?.ask, digits)}</span>
              </div>
              <p className="mt-1 text-[11px] text-mist-500">tick {localTime(quote?.time)}</p>
            </Panel>
            <Panel className="p-4">
              <div className="flex items-start justify-between">
                <p className="label">Spread</p>
                <Gauge className="h-4 w-4 text-mist-600" aria-hidden />
              </div>
              <p className={`mt-2 font-mono text-lg tabular-nums ${data.spread.ok ? "text-signal-up" : "text-signal-down"}`}>
                {num(data.spread.points, 1)} pts
              </p>
              <p className="mt-1 text-[11px] text-mist-500">
                limit {data.spread.limit_points === null ? "not configured" : `${num(data.spread.limit_points, 1)} pts`}
              </p>
            </Panel>
            <Panel className="p-4">
              <div className="flex items-start justify-between">
                <p className="label">Session</p>
                <CalendarClock className="h-4 w-4 text-mist-600" aria-hidden />
              </div>
              <p className="mt-2 font-display text-base uppercase tracking-wide">{data.session}</p>
              <p className="mt-1 text-[11px] text-mist-500">broker server timezone</p>
            </Panel>
            <Panel className="p-4">
              <div className="flex items-start justify-between">
                <p className="label">Regime</p>
                <Activity className="h-4 w-4 text-mist-600" aria-hidden />
              </div>
              <p className="mt-2 font-display text-base uppercase tracking-wide">{features?.regime ? titleCase(features.regime) : "—"}</p>
              <p className="mt-1 text-[11px] text-mist-500">{features?.structure ?? "no structure read"}</p>
            </Panel>
          </div>

          {data.reasons.length ? (
            <div className="border border-signal-warn/35 bg-signal-warn/5 px-4 py-3">
              <p className="text-xs font-semibold text-signal-warn">Why this is not tradable</p>
              <ul className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                {data.reasons.map((reason) => (
                  <li key={reason} className="flex items-center gap-1.5 text-[11px] text-mist-400">
                    <TriangleAlert className="h-3 w-3 text-signal-warn" aria-hidden />{reason}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <Panel>
            <PanelHeader
              title={`${data.symbol} · ${data.timeframe}`}
              eyebrow="Closed candles · EMA 20 / EMA 50"
              action={<Badge tone="gold">{data.candles.length} bars</Badge>}
            />
            {data.candles.length === 0 ? (
              <EmptyState
                icon={<Activity className="h-6 w-6" aria-hidden />}
                title="No candles"
                detail={data.candle_error ?? "MT5 returned no closed bars for this symbol and timeframe."}
              />
            ) : (
              <div className="p-3">
                <CandleChart candles={data.candles} forecast={forecast} digits={digits} />
                {forecastNote ? (
                  <p className="mt-2 text-[10px] text-signal-warn">{forecastNote}</p>
                ) : null}
                {data.candle_error ? (
                  <p className="mt-2 text-[10px] text-signal-warn">Candle note: {data.candle_error}</p>
                ) : null}
              </div>
            )}
          </Panel>

          <div className="grid items-start gap-4 xl:grid-cols-[1.15fr_0.85fr]">
            <div className="space-y-4">
              <Panel>
                <PanelHeader title="Deterministic features" eyebrow="Computed from the closed-candle window" />
                {!features?.available ? (
                  <EmptyState
                    icon={<Gauge className="h-6 w-6" aria-hidden />}
                    title="Feature window unavailable"
                    detail={features?.reason ?? "Fewer than 30 closed bars were returned."}
                  />
                ) : (
                  <div className="grid grid-cols-2 gap-4 p-4 sm:grid-cols-3">
                    <KeyValue label="Last close" value={num(features.last_close, digits)} />
                    <KeyValue label="EMA 20" value={num(features.ema_fast, digits)} />
                    <KeyValue label="EMA 50" value={num(features.ema_slow, digits)} />
                    <KeyValue label="RSI 14" value={num(features.rsi, 1)} tone={rsiTone(features.rsi)} />
                    <KeyValue label="ATR 14" value={num(features.atr, digits)} />
                    <KeyValue label="Bars used" value={String(features.bars ?? "—")} />
                    <KeyValue label="20-bar high" value={num(features.range_high, digits)} />
                    <KeyValue label="20-bar low" value={num(features.range_low, digits)} />
                    <KeyValue label="Structure" value={features.structure ?? "—"} />
                  </div>
                )}
              </Panel>

              <Panel>
                <PanelHeader
                  title="News calendar"
                  eyebrow="Configured high-impact window"
                  action={
                    <Badge tone={!news ? "idle" : news.blocked ? "bad" : news.available ? "ok" : "warn"}>
                      {!news ? "NO DATA" : news.blocked ? "BLOCKING" : news.available ? "CLEAR" : "UNAVAILABLE"}
                    </Badge>
                  }
                />
                {!news ? (
                  <EmptyState icon={<Newspaper className="h-6 w-6" aria-hidden />} title="No news block" />
                ) : !news.available ? (
                  <EmptyState
                    icon={<Newspaper className="h-6 w-6" aria-hidden />}
                    title="Calendar unavailable"
                    detail={news.reason ?? "No calendar source returned data."}
                  />
                ) : news.enabled === false ? (
                  <EmptyState
                    icon={<Newspaper className="h-6 w-6" aria-hidden />}
                    title="News filter disabled"
                    detail="news.enabled is false in config.json, so no event can block a trade."
                  />
                ) : (
                  <div className="space-y-3 p-4">
                    {news.warning ? (
                      <p className="border border-signal-warn/35 bg-signal-warn/5 px-3 py-2 text-[11px] text-signal-warn">{news.warning}</p>
                    ) : null}
                    <p className="font-mono text-[10px] text-mist-600">
                      calendar age {age(news.calendar_age_seconds)} · {newsEvents.length} event(s) in the active window
                    </p>
                    {newsEvents.length ? (
                      <ul className="divide-y divide-ink-700/50 border border-ink-700/60">
                        {newsEvents.map((event, index) => (
                          <li key={`${event.ts}-${index}`} className="flex items-center justify-between gap-2 px-3 py-2">
                            <div className="min-w-0">
                              <p className="truncate text-xs text-mist-200">{event.title ?? "untitled event"}</p>
                              <p className="font-mono text-[10px] text-mist-600">{localTime(event.ts)} · {event.currency ?? "—"}</p>
                            </div>
                            <Badge tone="bad">{titleCase(event.impact ?? "high")}</Badge>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-[11px] text-mist-500">No high-impact event inside the blocking window.</p>
                    )}
                    {upcoming.length ? (
                      <div>
                        <p className="label mb-1.5">Upcoming</p>
                        <ul className="space-y-1">
                          {upcoming.slice(0, 6).map((event, index) => (
                            <li key={`${event.ts}-${index}`} className="flex items-center justify-between gap-2 font-mono text-[10px] text-mist-500">
                              <span className="truncate">{localTime(event.ts)} · {event.title ?? "untitled"}</span>
                              <span className="shrink-0">{event.currency ?? "—"} · {titleCase(event.impact ?? "low")}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                  </div>
                )}
              </Panel>
            </div>

            <Panel>
              <PanelHeader
                title="Kronos forecast"
                eyebrow="Cached ensemble path"
                action={
                  <Badge tone={!kronos?.available ? "idle" : kronos.stale ? "warn" : "gold"}>
                    {!kronos?.available ? "UNAVAILABLE" : kronos.stale ? "STALE" : "FRESH"}
                  </Badge>
                }
              />
              {!kronos?.available ? (
                <EmptyState
                  icon={<Sparkles className="h-6 w-6" aria-hidden />}
                  title="No forecast"
                  detail={kronos?.reason ?? "No Kronos forecast is cached for this symbol."}
                />
              ) : (
                <div className="space-y-3 p-4">
                  {kronos.warning ? (
                    <p className="border border-signal-warn/35 bg-signal-warn/5 px-3 py-2 text-[11px] leading-relaxed text-signal-warn">
                      {kronos.warning}
                    </p>
                  ) : null}
                  <div className="grid grid-cols-2 gap-3">
                    <KeyValue label="Direction" value={directionLabel(kronos.direction)} />
                    <KeyValue label="Timeframe" value={kronos.timeframe ?? "—"} />
                    <KeyValue label="Move (price)" value={kronosValue(kronos.move, kronos.base_close, digits)} />
                    <KeyValue label="Next bar" value={kronosValue(kronos.move_next, kronos.base_close, digits)} />
                    <KeyValue label="Path high" value={kronosValue(kronos.path_high, kronos.base_close, digits)} />
                    <KeyValue label="Path low" value={kronosValue(kronos.path_low, kronos.base_close, digits)} />
                    <KeyValue label="OOD" value={kronos.ood === null || kronos.ood === undefined ? "—" : kronos.ood ? "yes" : "no"} />
                    <KeyValue label="Age" value={age(kronos.age_seconds)} />
                  </div>
                  {kronos.summary ? (
                    <p className="text-[11px] leading-relaxed text-mist-400">{kronos.summary}</p>
                  ) : null}
                  <div className="border border-ink-700/60 bg-ink-950/40 px-3 py-2">
                    <p className="label">Source</p>
                    <p className="mt-1 break-all font-mono text-[10px] text-mist-500">{kronos.source ?? "—"}</p>
                  </div>
                  <p className="text-[10px] leading-relaxed text-mist-600">
                    A Kronos path is context, never a trade authority. The committee and risk governor must still agree,
                    and a stale cache is shown as stale.
                  </p>
                  {rawForecast.length === 0 ? (
                    <p className="text-[11px] text-mist-600">The cache has no forecast candles to draw.</p>
                  ) : !forecastAligned || kronos.stale ? (
                    <p className="text-[11px] text-mist-600">
                      The cached path is not drawn on the chart — see the note under it.
                    </p>
                  ) : null}
                </div>
              )}
            </Panel>
          </div>

          <p className="flex items-center gap-2 px-1 text-[10px] uppercase tracking-[0.14em] text-mist-600">
            <Compass className="h-3 w-3" aria-hidden />
            Quote, candles, features, forecast and calendar age come from MT5 and the local cache. Nothing is smoothed or back-filled.
          </p>
        </>
      )}
    </div>
  );
}
