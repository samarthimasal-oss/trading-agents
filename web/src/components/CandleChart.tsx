import { useMemo, useState } from "react";

import type { Candle } from "../lib/types";
import { localTime, num } from "../lib/format";

const WIDTH = 900;
const HEIGHT = 320;
const PAD = { left: 10, right: 66, top: 14, bottom: 26 };

function ema(values: number[], period: number): Array<number | null> {
  if (values.length < period) return values.map(() => null);
  const k = 2 / (period + 1);
  let current = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  const out: Array<number | null> = values.map(() => null);
  out[period - 1] = current;
  for (let index = period; index < values.length; index += 1) {
    current = values[index] * k + current * (1 - k);
    out[index] = current;
  }
  return out;
}

function pathFor(
  values: Array<number | null>,
  xAt: (index: number) => number,
  yAt: (value: number) => number,
): string {
  let path = "";
  let open = false;
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      open = false;
      return;
    }
    path += `${open ? "L" : "M"}${xAt(index).toFixed(1)} ${yAt(value).toFixed(1)} `;
    open = true;
  });
  return path.trim();
}

function chartTime(ts: number | undefined): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function CandleChart({
  candles,
  forecast = [],
  digits = 2,
  className = "",
}: {
  candles: Candle[];
  forecast?: number[][];
  digits?: number;
  className?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);

  const chart = useMemo(() => {
    const history = candles.filter((bar) =>
      [bar.open, bar.high, bar.low, bar.close].every((value) => Number.isFinite(Number(value))),
    );
    const future: Candle[] = forecast
      .filter((row) => Array.isArray(row) && row.length >= 5)
      .map((row) => ({
        time: Number(row[0]),
        open: Number(row[1]),
        high: Number(row[2]),
        low: Number(row[3]),
        close: Number(row[4]),
      }));
    const bars = [...history, ...future];
    if (bars.length < 2) {
      return { bars, history, future, xAt: () => 0, yAt: () => 0, ema20: [], ema50: [], lo: 0, hi: 0, step: 0 };
    }
    const closes = history.map((bar) => bar.close);
    const ema20 = ema(closes, 20);
    const ema50 = ema(closes, 50);
    const values = [
      ...bars.flatMap((bar) => [bar.high, bar.low]),
      ...ema20.filter((value): value is number => value !== null),
      ...ema50.filter((value): value is number => value !== null),
    ];
    let lo = Math.min(...values);
    let hi = Math.max(...values);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || lo === hi) {
      const last = bars[bars.length - 1]?.close ?? 0;
      lo = last - 1;
      hi = last + 1;
    }
    const padding = (hi - lo) * 0.06;
    lo -= padding;
    hi += padding;
    const plotWidth = WIDTH - PAD.left - PAD.right;
    const plotHeight = HEIGHT - PAD.top - PAD.bottom;
    const step = plotWidth / bars.length;
    const xAt = (index: number) => PAD.left + (index + 0.5) * step;
    const yAt = (value: number) => PAD.top + ((hi - value) / (hi - lo)) * plotHeight;
    return { bars, history, future, xAt, yAt, ema20, ema50, lo, hi, step };
  }, [candles, forecast]);

  if (chart.bars.length < 2) {
    return (
      <div className={`flex h-[260px] items-center justify-center text-xs text-mist-600 ${className}`}>
        No closed candles available for this timeframe.
      </div>
    );
  }

  const { bars, history, future, xAt, yAt, ema20, ema50, lo, hi, step } = chart;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const bodyWidth = Math.max(1.4, Math.min(9, step * 0.62));
  const lastHistory = history.length - 1;
  const lastClose = history[lastHistory]?.close;
  const ticks = Array.from({ length: 5 }, (_, index) => lo + ((hi - lo) * index) / 4);
  const labelIndexes = Array.from({ length: 5 }, (_, index) =>
    Math.round((index * (bars.length - 1)) / 4),
  ).filter((value, index, array) => array.indexOf(value) === index);
  const hovered = hover === null ? null : bars[hover];
  const hoverX = hover === null ? 0 : xAt(hover);
  const hoverLeft = Math.min(88, Math.max(12, (hoverX / WIDTH) * 100));

  return (
    <div className={`relative ${className}`}>
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2 px-1">
        <div className="flex flex-wrap items-center gap-3 text-[9px] uppercase tracking-[0.14em] text-mist-600">
          <span className="flex items-center gap-1"><i className="h-px w-4 bg-[#74a7d8]" />EMA 20</span>
          <span className="flex items-center gap-1"><i className="h-px w-4 bg-[#d9ae62]" />EMA 50</span>
          {future.length ? (
            <span className="flex items-center gap-1 text-gold-300/80">
              <i className="h-2 w-2 border border-gold-400/70 bg-gold-500/10" />Kronos forecast
            </span>
          ) : null}
        </div>
        <span className="font-mono text-[10px] text-mist-500">
          {history.length} bars{future.length ? ` · +${future.length} forecast` : ""}
        </span>
      </div>

      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-[300px] w-full"
        role="img"
        aria-label={`${history.length} closed candles with EMA 20 and EMA 50. Last close ${num(lastClose, digits)}.`}
        onMouseMove={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          const x = ((event.clientX - rect.left) / rect.width) * WIDTH;
          const index = Math.round((x - PAD.left) / step - 0.5);
          setHover(index >= 0 && index < bars.length ? index : null);
        }}
        onMouseLeave={() => setHover(null)}
      >
        <rect x={0} y={0} width={WIDTH} height={HEIGHT} fill="transparent" />
        {ticks.map((value) => (
          <g key={`tick-${value}`}>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={yAt(value)}
              y2={yAt(value)}
              stroke="#1f2934"
              strokeDasharray="2 5"
            />
            <text
              x={WIDTH - PAD.right + 8}
              y={yAt(value) + 3}
              fill="#647181"
              fontSize={10}
              fontFamily="Cascadia Mono, monospace"
            >
              {num(value, digits)}
            </text>
          </g>
        ))}

        {labelIndexes.map((index) => (
          <text
            key={`time-${index}`}
            x={xAt(index)}
            y={HEIGHT - 8}
            fill="#647181"
            fontSize={9}
            fontFamily="Cascadia Mono, monospace"
            textAnchor={index === 0 ? "start" : index === bars.length - 1 ? "end" : "middle"}
          >
            {chartTime(bars[index]?.time)}
          </text>
        ))}

        {bars.map((bar, index) => {
          const rising = bar.close >= bar.open;
          const color = rising ? "#5ccf9b" : "#e06c75";
          const x = xAt(index);
          const top = yAt(Math.max(bar.open, bar.close));
          const bottom = yAt(Math.min(bar.open, bar.close));
          const isFuture = index > lastHistory;
          return (
            <g key={`${bar.time}-${index}`} opacity={isFuture ? 0.8 : 1}>
              <line
                x1={x}
                x2={x}
                y1={yAt(bar.high)}
                y2={yAt(bar.low)}
                stroke={isFuture ? "#d9ae62" : color}
                strokeWidth={isFuture ? 1 : 1.1}
                strokeDasharray={isFuture ? "2 2" : undefined}
              />
              <rect
                x={x - bodyWidth / 2}
                y={top}
                width={bodyWidth}
                height={Math.max(1, bottom - top)}
                fill={isFuture ? "rgba(217,174,98,0.12)" : color}
                stroke={isFuture ? "#d9ae62" : color}
                strokeWidth={isFuture ? 0.8 : 0}
              />
            </g>
          );
        })}

        {lastClose !== undefined ? (
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={yAt(lastClose)}
            y2={yAt(lastClose)}
            stroke="#d9ae62"
            strokeDasharray="3 4"
            opacity={0.7}
          />
        ) : null}

        {future.length ? (
          <line
            x1={xAt(lastHistory) + step / 2}
            x2={xAt(lastHistory) + step / 2}
            y1={PAD.top}
            y2={PAD.top + plotHeight}
            stroke="#d9ae62"
            strokeDasharray="2 4"
            opacity={0.45}
          />
        ) : null}

        <path d={pathFor(ema20, xAt, yAt)} fill="none" stroke="#74a7d8" strokeWidth={1.2} opacity={0.8} />
        <path d={pathFor(ema50, xAt, yAt)} fill="none" stroke="#d9ae62" strokeWidth={1.2} opacity={0.8} />

        {lastClose !== undefined ? (
          <>
            <rect
              x={WIDTH - PAD.right + 2}
              y={yAt(lastClose) - 8}
              width={PAD.right - 5}
              height={16}
              fill="#d9ae62"
            />
            <text
              x={WIDTH - PAD.right + 6}
              y={yAt(lastClose) + 4}
              fill="#080a0d"
              fontSize={10}
              fontFamily="Cascadia Mono, monospace"
              fontWeight={600}
            >
              {num(lastClose, digits)}
            </text>
          </>
        ) : null}

        {hover !== null ? (
          <line
            x1={xAt(hover)}
            x2={xAt(hover)}
            y1={PAD.top}
            y2={PAD.top + plotHeight}
            stroke="#647181"
            strokeDasharray="2 3"
            opacity={0.65}
          />
        ) : null}
      </svg>

      {hovered ? (
        <div
          className="pointer-events-none absolute top-7 z-10 min-w-[150px] border border-ink-600 bg-ink-950/95 px-2.5 py-2 shadow-panel"
          style={{ left: `${hoverLeft}%`, transform: "translateX(-50%)" }}
        >
          <p className="font-mono text-[9px] uppercase tracking-[0.1em] text-mist-500">
            {localTime(hovered.time)}
            {hover !== null && hover > lastHistory ? " · forecast" : ""}
          </p>
          <div className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[10px] text-mist-300">
            <span className="text-mist-600">O</span><span className="text-right">{num(hovered.open, digits)}</span>
            <span className="text-mist-600">H</span><span className="text-right">{num(hovered.high, digits)}</span>
            <span className="text-mist-600">L</span><span className="text-right">{num(hovered.low, digits)}</span>
            <span className="text-mist-600">C</span>
            <span className={`text-right ${hovered.close >= hovered.open ? "text-signal-up" : "text-signal-down"}`}>
              {num(hovered.close, digits)}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
