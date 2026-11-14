import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { OctagonX, RotateCcw, Save, ShieldAlert, ShieldCheck } from "lucide-react";

import { api, ApiError } from "../lib/api";
import type { RiskLimits, RiskSnapshot, RunnerStatus } from "../lib/types";
import { age, localTime, money, num, signed, toneForNumber } from "../lib/format";
import { Badge, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader, StatusDot } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { RunnerControl } from "../components/RunnerControl";
import { useToast } from "../components/Toast";

interface FieldDef {
  key: keyof RiskLimits;
  label: string;
  group: "sizing" | "exposure" | "management";
  step?: number;
  suffix?: string;
}

const FIELDS: FieldDef[] = [
  { key: "risk_percent_per_trade", label: "Risk per trade", group: "sizing", step: 0.05, suffix: "%" },
  { key: "fixed_lot", label: "Fixed lot (0 = dynamic)", group: "sizing", step: 0.01 },
  { key: "max_notional_leverage_per_position", label: "Max notional leverage", group: "sizing", step: 0.1, suffix: "×" },
  { key: "max_positions", label: "Max positions", group: "exposure", step: 1 },
  { key: "max_open_same_symbol", label: "Max per symbol", group: "exposure", step: 1 },
  { key: "max_daily_loss_percent", label: "Daily loss lockout", group: "exposure", step: 0.1, suffix: "%" },
  { key: "max_equity_drawdown_percent", label: "Max equity drawdown", group: "exposure", step: 0.1, suffix: "%" },
  { key: "max_spread_points", label: "Max spread", group: "exposure", step: 1, suffix: "pts" },
  { key: "partial_close_at_r", label: "Partial at", group: "management", step: 0.1, suffix: "R" },
  { key: "partial_close_pct", label: "Partial size", group: "management", step: 1, suffix: "%" },
  { key: "time_stop_minutes", label: "Time stop", group: "management", step: 5, suffix: "min" },
  { key: "breakeven_atr", label: "Breakeven at", group: "management", step: 0.1, suffix: "ATR" },
  { key: "trailing_atr", label: "Trailing distance", group: "management", step: 0.1, suffix: "ATR" },
  { key: "trailing_step_atr", label: "Trailing step", group: "management", step: 0.01, suffix: "ATR" },
];

type FormState = Record<string, number | boolean>;

function formFrom(limits: RiskLimits): FormState {
  const state: FormState = {};
  for (const field of FIELDS) {
    const value = limits[field.key];
    state[String(field.key)] = typeof value === "boolean" ? value : Number(value ?? 0);
  }
  state.partial_close_enabled = Boolean(limits.partial_close_enabled);
  return state;
}

function isRiskSnapshot(value: RiskSnapshot | { available: false; reason: string } | undefined): value is RiskSnapshot {
  return Boolean(value && typeof value === "object" && "limits" in value);
}

export function RiskCentrePage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const query = useQuery({ queryKey: ["risk"], queryFn: api.risk, refetchInterval: 15_000 });
  const runnerQuery = useQuery({ queryKey: ["runner"], queryFn: api.runner, refetchInterval: 10_000 });

  const [form, setForm] = useState<FormState>({});
  const [dirty, setDirty] = useState(false);
  const [dryDialog, setDryDialog] = useState(false);
  const [realDialog, setRealDialog] = useState(false);
  const [resetDialog, setResetDialog] = useState(false);
  const [lastResult, setLastResult] = useState<Record<string, unknown> | null>(null);

  const snapshot = isRiskSnapshot(query.data) ? query.data : null;
  const runner: RunnerStatus | undefined = runnerQuery.data?.runner ?? snapshot?.runner;
  const runnerActive = Boolean(runner?.pid) && runner?.mode !== "stopped";

  useEffect(() => {
    if (snapshot && !dirty) setForm(formFrom(snapshot.limits));
  }, [snapshot, dirty]);

  const patch = useMemo(() => {
    if (!snapshot) return {};
    const changed: Record<string, unknown> = {};
    for (const field of FIELDS) {
      const key = String(field.key);
      const current = snapshot.limits[field.key];
      if (form[key] !== undefined && Number(form[key]) !== Number(current ?? 0)) {
        changed[`risk.${key}`] = Number(form[key]);
      }
    }
    if (Boolean(form.partial_close_enabled) !== Boolean(snapshot.limits.partial_close_enabled)) {
      changed["risk.partial_close_enabled"] = Boolean(form.partial_close_enabled);
    }
    return changed;
  }, [form, snapshot]);

  const save = useMutation({
    mutationFn: () => api.updateSettings(patch),
    onSuccess: () => {
      toast.push("success", "Risk limits saved", "Restart the runner for changes to take effect.");
      setDirty(false);
      void queryClient.invalidateQueries({ queryKey: ["risk"] });
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => {
      toast.push("error", "Limits rejected", error instanceof ApiError ? error.message : String(error));
    },
  });

  const dryClose = useMutation({
    mutationFn: () => api.emergencyClose(true, ""),
    onSuccess: (result) => {
      setLastResult(result as unknown as Record<string, unknown>);
      setDryDialog(false);
      toast.push("info", "Dry-run close simulated", result.message);
    },
    onError: (error) => {
      setDryDialog(false);
      toast.push("error", "Dry-run failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  const realClose = useMutation({
    mutationFn: () => api.emergencyClose(false, "CLOSE ALL"),
    onSuccess: (result) => {
      setLastResult(result as unknown as Record<string, unknown>);
      setRealDialog(false);
      toast.push("success", "Emergency close sent", result.message);
      void queryClient.invalidateQueries({ queryKey: ["risk"] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
    },
    onError: (error) => {
      setRealDialog(false);
      toast.push("error", "Emergency close failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  const reset = useMutation({
    mutationFn: api.resetGuardian,
    onSuccess: () => {
      setResetDialog(false);
      toast.push("success", "Guardian state cleared", "The halt flag was removed for today.");
      void queryClient.invalidateQueries({ queryKey: ["risk"] });
    },
    onError: (error) => {
      setResetDialog(false);
      toast.push("error", "Reset failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  if (query.isLoading) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <LoadingBlock label="Reading the risk snapshot" />
        </Panel>
      </div>
    );
  }

  if (query.isError || !snapshot) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          <PanelHeader title="Risk snapshot unavailable" eyebrow="Governor" danger />
          <div className="p-4">
            <ErrorNote
              title="The governor could not read the account."
              detail={
                query.error instanceof Error
                  ? query.error.message
                  : (query.data as { reason?: string } | undefined)?.reason ?? "MT5 is not connected."
              }
            />
            <button type="button" className="btn btn-ghost mt-3" onClick={() => void query.refetch()}>
              Retry
            </button>
          </div>
        </Panel>
      </div>
    );
  }

  const guardian = snapshot.guardian;
  const limitUsd = Number(snapshot.daily_loss_limit_usd ?? 0);
  const pnl = Number(snapshot.daily_pnl ?? 0);
  const usedPct = limitUsd > 0 ? Math.min(999, Math.max(0, (-pnl / limitUsd) * 100)) : null;

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <Panel className="p-4">
          <p className="label">Open positions</p>
          <p className="mt-2 font-mono text-2xl tabular-nums">{snapshot.open_positions}</p>
          <p className="mt-1 text-[11px] text-mist-500">magic {snapshot.magic ?? "—"}</p>
        </Panel>
        <Panel className="p-4">
          <p className="label">Daily P/L</p>
          <p className={`mt-2 font-mono text-2xl tabular-nums ${toneForNumber(pnl)}`}>
            {snapshot.daily_pnl === null || snapshot.daily_pnl === undefined ? "—" : signed(pnl)}
          </p>
          <p className="mt-1 text-[11px] text-mist-500">loss budget {money(limitUsd)}</p>
        </Panel>
        <Panel className="p-4">
          <p className="label">Loss budget used</p>
          <p className="mt-2 font-mono text-2xl tabular-nums">{usedPct === null ? "—" : `${num(usedPct, 0)}%`}</p>
          <div className="mt-2 h-1 w-full bg-ink-700">
            <div
              className="h-1 bg-signal-warn"
              style={{ width: `${Math.min(100, usedPct ?? 0)}%` }}
            />
          </div>
        </Panel>
        <Panel className="p-4">
          <p className="label">Peak equity · journal</p>
          <p className="mt-2 font-mono text-2xl tabular-nums">{money(snapshot.peak_equity)}</p>
          <p className="mt-1 text-[11px] text-mist-500">
            drawdown {snapshot.drawdown_from_peak_pct === null || snapshot.drawdown_from_peak_pct === undefined
              ? "—"
              : `${num(snapshot.drawdown_from_peak_pct)}%`}
            {snapshot.peak_equity_ts ? ` · ${localTime(snapshot.peak_equity_ts)}` : ""}
          </p>
        </Panel>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.45fr_0.55fr]">
        <Panel>
          <PanelHeader
            title="Risk limits"
            eyebrow="config.json · requires restart"
            action={
              <button
                type="button"
                className="btn btn-primary"
                disabled={save.isPending || Object.keys(patch).length === 0 || runnerActive}
                onClick={() => save.mutate()}
                title={runnerActive ? "Stop the runner before changing limits" : undefined}
              >
                <Save className="h-3.5 w-3.5" aria-hidden />
                {save.isPending ? "Saving…" : "Save limits"}
              </button>
            }
          />

          {runnerActive ? (
            <p className="border-b border-signal-warn/25 bg-signal-warn/5 px-4 py-2 text-[11px] text-signal-warn">
              Runner PID {runner?.pid} is active. Stop it before editing risk limits.
            </p>
          ) : null}

          <div className="space-y-5 p-4">
            {(["sizing", "exposure", "management"] as const).map((group) => (
              <div key={group}>
                <p className="eyebrow">
                  {group === "sizing" ? "Position sizing" : group === "exposure" ? "Exposure & lockouts" : "Protective management"}
                </p>
                <div className="mt-2 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                  {FIELDS.filter((field) => field.group === group).map((field) => {
                    const key = String(field.key);
                    const value = form[key];
                    return (
                      <label key={key} className="block">
                        <span className="label flex items-center justify-between gap-2">
                          <span>{field.label}</span>
                          {field.suffix ? <span className="text-mist-600">{field.suffix}</span> : null}
                        </span>
                        <input
                          type="number"
                          className="input mt-1.5"
                          value={typeof value === "boolean" ? Number(value) : value ?? 0}
                          step={field.step}
                          disabled={runnerActive}
                          onChange={(event) => {
                            setForm((current) => ({ ...current, [key]: Number(event.target.value) }));
                            setDirty(true);
                          }}
                        />
                      </label>
                    );
                  })}
                </div>
              </div>
            ))}

            <label className="flex items-center gap-2.5 border border-ink-700/70 bg-ink-850/50 px-3 py-2.5">
              <input
                type="checkbox"
                className="h-4 w-4 accent-gold-500"
                checked={Boolean(form.partial_close_enabled)}
                disabled={runnerActive}
                onChange={(event) => {
                  setForm((current) => ({ ...current, partial_close_enabled: event.target.checked }));
                  setDirty(true);
                }}
              />
              <span className="text-sm text-mist-300">Take a partial at the configured R multiple</span>
            </label>

            <div className="hairline flex flex-wrap items-center justify-between gap-2 pt-3">
              <p className="text-[10px] leading-relaxed text-mist-600">
                Changes are validated and backed up server-side. Secrets cannot be edited here.
              </p>
              <span className="text-[10px] uppercase tracking-[0.14em] text-mist-600">
                {dirty ? `${Object.keys(patch).length} unsaved change(s)` : "in sync"}
              </span>
            </div>
          </div>
        </Panel>

        <div className="space-y-4">
          <Panel>
            <PanelHeader
              title="Guardian"
              eyebrow="Independent halt"
              action={
                <Badge tone={guardian?.halted_day ? "bad" : guardian?.available ? "ok" : "idle"}>
                  {guardian?.halted_day ? "HALTED" : guardian?.available ? "ARMED" : "NO STATE"}
                </Badge>
              }
            />
            <div className="space-y-3 p-4">
              <KeyValue label="Trading day" value={guardian?.day ?? "—"} />
              <KeyValue label="Day start equity" value={money(guardian?.day_start_equity)} />
              <KeyValue
                label="Halt"
                value={guardian?.halted_day ? `${guardian.halted_day} · ${age(guardian.halted_at ? (Date.now() / 1000) - guardian.halted_at : null)}` : "none"}
              />
              <p className="text-[11px] leading-relaxed text-mist-500">
                {guardian?.halt_reason || "No halt reason is recorded for the current state."}
              </p>
              <button
                type="button"
                className="btn btn-ghost w-full"
                disabled={!guardian?.halted_day || runnerActive}
                onClick={() => setResetDialog(true)}
                title={!guardian?.halted_day ? "The guardian is not halted" : undefined}
              >
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
                Clear halt
              </button>
            </div>
          </Panel>

          <RunnerControl runner={runner} />
        </div>
      </div>

      <Panel>
        <PanelHeader
          title="Emergency close-all"
          eyebrow="Independent of the runner"
          danger
          action={<Badge tone="bad">destructive</Badge>}
        />
        <div className="grid gap-4 p-4 lg:grid-cols-[1.2fr_0.8fr]">
          <div className="space-y-3">
            <div className="flex items-start gap-2.5">
              <OctagonX className="mt-0.5 h-5 w-5 shrink-0 text-signal-down" aria-hidden />
              <p className="text-sm leading-relaxed text-mist-300">
                Closes every position carrying the bot magic number. Always dry-run first. The real path requires
                typing <span className="font-mono text-gold-300">CLOSE ALL</span> and cannot be triggered by
                the runner.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={dryClose.isPending}
                onClick={() => setDryDialog(true)}
              >
                <ShieldCheck className="h-3.5 w-3.5" aria-hidden />
                Dry-run close
              </button>
              <button
                type="button"
                className="btn btn-danger"
                disabled={realClose.isPending}
                onClick={() => setRealDialog(true)}
              >
                <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
                Close all positions
              </button>
            </div>
          </div>

          <div className="border border-ink-700/70 bg-ink-950/60 p-3">
            <p className="label">Last result</p>
            {lastResult ? (
              <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-mist-300">
                {JSON.stringify(lastResult, null, 2)}
              </pre>
            ) : (
              <p className="mt-2 text-[11px] leading-relaxed text-mist-600">
                No close has been attempted in this session.
              </p>
            )}
            <div className="mt-3 flex items-center gap-2">
              <StatusDot tone={snapshot.account?.available === false ? "bad" : "ok"} />
              <p className="text-[10px] text-mist-600">
                MT5 {snapshot.account?.available === false ? "unavailable" : "connected"} ·{" "}
                {localTime(snapshot.account?.available === false ? null : Date.now() / 1000)}
              </p>
            </div>
          </div>
        </div>
      </Panel>

      <ConfirmDialog
        open={dryDialog}
        title="Dry-run close-all"
        description="This counts the bot positions and reports what would be closed. No order is sent to MT5."
        confirmLabel="Run simulation"
        busy={dryClose.isPending}
        onConfirm={() => dryClose.mutate()}
        onCancel={() => setDryDialog(false)}
      />
      <ConfirmDialog
        open={realDialog}
        title="Close every bot position"
        description="This sends real market orders on the demo account. Positions opened manually are not touched. Type the phrase to arm the action."
        confirmLabel="Send close orders"
        tone="danger"
        requireText="CLOSE ALL"
        busy={realClose.isPending}
        onConfirm={() => realClose.mutate()}
        onCancel={() => setRealDialog(false)}
      />
      <ConfirmDialog
        open={resetDialog}
        title="Clear the guardian halt"
        description="Removes today's halt flag so the runner can trade again. Only do this after you understand why it halted."
        confirmLabel="Clear halt"
        tone="danger"
        requireText="RESET"
        busy={reset.isPending}
        onConfirm={() => reset.mutate()}
        onCancel={() => setResetDialog(false)}
      />
    </div>
  );
}
