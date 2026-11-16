import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "../lib/api";

/**
 * Symbol list for the market/execution pickers.
 *
 * The configured trading symbols come first, then the default symbol, then a
 * small broker-agnostic fallback.  Nothing here implies the broker offers a
 * symbol; the quote request reports that honestly.
 */
export function useSymbols(): string[] {
  const query = useQuery({ queryKey: ["settings"], queryFn: api.settings, staleTime: 60_000 });

  return useMemo(() => {
    const trading = (query.data?.settings?.trading ?? {}) as Record<string, unknown>;
    const raw = trading.symbols;
    const configured = Array.isArray(raw)
      ? raw.map((item) => String(item).toUpperCase()).filter(Boolean)
      : [];
    const fallback = String(trading.symbol ?? "XAUUSD").toUpperCase();
    return Array.from(new Set([fallback, ...configured, "XAUUSD", "EURUSD", "GBPUSD"]));
  }, [query.data]);
}
