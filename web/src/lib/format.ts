export const EM_DASH = "—";

export function money(value: unknown, currency = "USD"): string {
  if (value === null || value === undefined || value === "") return EM_DASH;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return EM_DASH;
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency,
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(numeric);
  } catch {
    return `${numeric.toFixed(2)} ${currency}`;
  }
}

export function num(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return EM_DASH;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return EM_DASH;
  return numeric.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function signed(value: unknown, digits = 2): string {
  if (value === null || value === undefined) return EM_DASH;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return EM_DASH;
  return `${numeric > 0 ? "+" : ""}${num(numeric, digits)}`;
}

export function pct(value: unknown, digits = 2): string {
  if (value === null || value === undefined) return EM_DASH;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return EM_DASH;
  return `${numeric.toFixed(digits)}%`;
}

export function rMultiple(value: unknown): string {
  if (value === null || value === undefined) return EM_DASH;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return EM_DASH;
  return `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)}R`;
}

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return EM_DASH;
  const value = Math.max(0, Number(seconds));
  if (value < 60) return `${Math.round(value)}s`;
  if (value < 3600) return `${Math.round(value / 60)}m`;
  if (value < 86400) return `${(value / 3600).toFixed(1)}h`;
  return `${(value / 86400).toFixed(1)}d`;
}

export function localTime(ts: number | null | undefined): string {
  if (!ts || !Number.isFinite(Number(ts))) return EM_DASH;
  return new Date(Number(ts) * 1000).toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function utcClock(date = new Date()): string {
  return date.toLocaleTimeString("en-GB", {
    timeZone: "UTC",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function shortTime(ts: number | null | undefined): string {
  if (!ts) return EM_DASH;
  return new Date(Number(ts) * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return EM_DASH;
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function toneForNumber(value: unknown): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "text-mist-300";
  if (numeric > 0) return "text-signal-up";
  if (numeric < 0) return "text-signal-down";
  return "text-mist-300";
}

export function sideLabel(side: string | undefined, type?: number): string {
  if (side && side !== "unknown") return side.toUpperCase();
  if (type === 0) return "BUY";
  if (type === 1) return "SELL";
  return EM_DASH;
}
