import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Save, ServerCog, ShieldCheck, SlidersHorizontal } from "lucide-react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../lib/api";
import type { PublicSettings, RunnerStatus, SettingsPayload, SystemInfo } from "../lib/types";
import { localTime, num } from "../lib/format";
import { Badge, ErrorNote, KeyValue, LoadingBlock, Panel, PanelHeader } from "../components/Panel";
import { SafetyBanner } from "../components/SafetyBanner";
import { useToast } from "../components/Toast";

interface Editable {
  path: string;
  label: string;
  kind: "text" | "number" | "boolean" | "symbols" | "select";
  options?: string[];
  step?: number;
  help?: string;
  wide?: boolean;
}

const EDITABLE: Editable[] = [
  { path: "trading.symbols", label: "Symbols", kind: "symbols", help: "Comma-separated, e.g. XAUUSD", wide: true },
  { path: "trading.timeframe", label: "Signal timeframe", kind: "select", options: ["M1", "M5", "M15", "M30", "H1", "H4", "D1"] },
  { path: "trading.close_on_opposite", label: "Close on opposite signal", kind: "boolean" },
  { path: "trading.require_llm_approval", label: "Require LLM approval", kind: "boolean" },
  { path: "llm.enabled", label: "LLM enabled", kind: "boolean" },
  { path: "llm.gemini_model", label: "Gemini model", kind: "text" },
  { path: "kronos.enabled", label: "Kronos enabled", kind: "boolean" },
  { path: "kronos.ensemble_runs", label: "Kronos ensemble runs", kind: "number", step: 1 },
  { path: "news.enabled", label: "News filter enabled", kind: "boolean" },
  { path: "news.window_before_min", label: "News window before", kind: "number", step: 1 },
  { path: "news.window_after_min", label: "News window after", kind: "number", step: 1 },
  { path: "telegram.enabled", label: "Telegram alerts enabled", kind: "boolean" },
];

type Value = string | number | boolean;

function getPath(settings: PublicSettings, path: string): unknown {
  const [section, key] = path.split(".");
  const group = settings[section as keyof PublicSettings] as Record<string, unknown> | undefined;
  return group?.[key];
}

function initialValue(settings: PublicSettings, field: Editable): Value {
  const raw = getPath(settings, field.path);
  if (field.kind === "boolean") return Boolean(raw);
  if (field.kind === "number") return Number(raw ?? 0);
  if (field.kind === "symbols") {
    return Array.isArray(raw) ? (raw as string[]).join(", ") : String(raw ?? "");
  }
  return String(raw ?? "");
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const query = useQuery<SettingsPayload>({ queryKey: ["settings"], queryFn: api.settings });
  const runnerQuery = useQuery({ queryKey: ["runner"], queryFn: api.runner, refetchInterval: 10_000 });
  const systemQuery = useQuery<SystemInfo>({ queryKey: ["system-info"], queryFn: api.systemInfo });

  const [form, setForm] = useState<Record<string, Value>>({});
  const [dirty, setDirty] = useState(false);

  const settings = query.data?.settings;
  const runner: RunnerStatus | undefined = runnerQuery.data?.runner;
  const runnerActive = Boolean(runner?.pid) && runner?.mode !== "stopped";

  useEffect(() => {
    if (!settings || dirty) return;
    const next: Record<string, Value> = {};
    for (const field of EDITABLE) next[field.path] = initialValue(settings, field);
    setForm(next);
  }, [settings, dirty]);

  const patch = useMemo(() => {
    if (!settings) return {};
    const changed: Record<string, unknown> = {};
    for (const field of EDITABLE) {
      const value = form[field.path];
      if (value === undefined) continue;
      const initial = initialValue(settings, field);
      if (field.kind === "symbols") {
        const list = String(value)
          .split(",")
          .map((item) => item.trim().toUpperCase())
          .filter(Boolean);
        const previous = String(initialValue(settings, field))
          .split(",")
          .map((item) => item.trim().toUpperCase())
          .filter(Boolean);
        if (list.join(",") !== previous.join(",")) changed[field.path] = list;
      } else if (String(value) !== String(initial)) {
        changed[field.path] = field.kind === "number" ? Number(value) : value;
      }
    }
    return changed;
  }, [form, settings]);

  const save = useMutation({
    mutationFn: () => api.updateSettings(patch),
    onSuccess: () => {
      toast.push("success", "Configuration saved", "A backup was written next to config.json. Restart the runner.");
      setDirty(false);
      void queryClient.invalidateQueries({ queryKey: ["settings"] });
      void queryClient.invalidateQueries({ queryKey: ["system-info"] });
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
    onError: (error) => {
      toast.push("error", "Configuration rejected", error instanceof ApiError ? error.message : String(error));
    },
  });

  if (query.isLoading || !settings) {
    return (
      <div className="space-y-4">
        <SafetyBanner />
        <Panel>
          {query.isError ? (
            <div className="p-4">
              <ErrorNote
                title="Could not read config.json"
                detail={query.error instanceof Error ? query.error.message : "unknown error"}
              />
            </div>
          ) : (
            <LoadingBlock label="Reading the allow-listed configuration" />
          )}
        </Panel>
      </div>
    );
  }

  const llm = settings.llm;
  const kronos = settings.kronos;
  const news = settings.news;
  const telegram = settings.telegram;
  const mt5 = settings.mt5;
  const ea = settings.ea;

  return (
    <div className="space-y-4 animate-rise">
      <SafetyBanner />

      <div className="flex flex-wrap items-center justify-between gap-3 border border-ink-700 bg-ink-900/60 px-4 py-3">
        <div className="flex items-center gap-2.5">
          <ShieldCheck className="h-4 w-4 text-gold-400" aria-hidden />
          <p className="text-[11px] leading-relaxed text-mist-500">
            Secrets are never rendered. Only allow-listed, non-secret fields are writable.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {runnerActive ? <Badge tone="warn">RUNNER ACTIVE</Badge> : null}
          <button
            type="button"
            className="btn btn-primary"
            disabled={save.isPending || Object.keys(patch).length === 0 || runnerActive}
            onClick={() => save.mutate()}
            title={runnerActive ? "Stop the runner before saving" : undefined}
          >
            <Save className="h-3.5 w-3.5" aria-hidden />
            {save.isPending ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.4fr_0.6fr]">
        <div className="space-y-4">
          <Panel>
            <PanelHeader
              title="Trading & reasoning"
              eyebrow="Writable · restart required"
              action={<Badge tone={dirty ? "warn" : "idle"}>{dirty ? `${Object.keys(patch).length} changed` : "in sync"}</Badge>}
            />
            {runnerActive ? (
              <p className="border-b border-signal-warn/25 bg-signal-warn/5 px-4 py-2 text-[11px] text-signal-warn">
                Runner PID {runner?.pid} is active. Stop it to edit settings.
              </p>
            ) : null}
            <div className="grid gap-4 p-4 sm:grid-cols-2">
              {EDITABLE.map((field) => {
                const value = form[field.path];
                return (
                  <label key={field.path} className={`block ${field.wide ? "sm:col-span-2" : ""}`}>
                    <span className="label">{field.label}</span>
                    {field.kind === "boolean" ? (
                      <span className="mt-2 flex items-center gap-2">
                        <input
                          type="checkbox"
                          className="h-4 w-4 accent-gold-500"
                          checked={Boolean(value)}
                          disabled={runnerActive}
                          onChange={(event) => {
                            setForm((current) => ({ ...current, [field.path]: event.target.checked }));
                            setDirty(true);
                          }}
                        />
                        <span className="text-xs text-mist-400">{value ? "Enabled" : "Disabled"}</span>
                      </span>
                    ) : field.kind === "select" ? (
                      <select
                        className="input mt-1.5"
                        value={String(value ?? "")}
                        disabled={runnerActive}
                        onChange={(event) => {
                          setForm((current) => ({ ...current, [field.path]: event.target.value }));
                          setDirty(true);
                        }}
                      >
                        {field.options?.map((option) => (
                          <option key={option} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        type={field.kind === "number" ? "number" : "text"}
                        className="input mt-1.5"
                        step={field.step}
                        value={String(value ?? "")}
                        disabled={runnerActive}
                        onChange={(event) => {
                          const next = field.kind === "number" ? Number(event.target.value) : event.target.value;
                          setForm((current) => ({ ...current, [field.path]: next }));
                          setDirty(true);
                        }}
                      />
                    )}
                    {field.help ? <span className="mt-1 block text-[10px] text-mist-600">{field.help}</span> : null}
                  </label>
                );
              })}
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="Risk limits" eyebrow="Moved to the Risk Centre" action={<Badge tone="gold">governor</Badge>} />
            <div className="flex flex-wrap items-center justify-between gap-3 p-4">
              <p className="max-w-xl text-[11px] leading-relaxed text-mist-500">
                Sizing, lockouts and protective management are edited from the dedicated Risk Centre, where the
                guardian state and emergency close live. This keeps destructive actions on one screen.
              </p>
              <Link className="btn btn-ghost" to="/risk">
                <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
                Open Risk Centre
              </Link>
            </div>
          </Panel>
        </div>

        <div className="space-y-4">
          <Panel>
            <PanelHeader title="Connections" eyebrow="Read-only" action={<ServerCog className="h-4 w-4 text-mist-600" aria-hidden />} />
            <div className="space-y-3 p-4">
              <KeyValue label="MT5 login" value={mt5.login ? String(mt5.login) : "terminal auto-login"} />
              <KeyValue label="Server" value={String(mt5.server ?? "—")} />
              <KeyValue label="Terminal" value={String(mt5.terminal_path ?? "auto")} />
              <div className="flex items-center justify-between">
                <span className="label">Password</span>
                <Badge tone={mt5.password_present ? "ok" : "warn"}>
                  {mt5.password_present ? "stored" : "missing"}
                </Badge>
              </div>
              <div className="hairline pt-3">
                <KeyValue label="EA host" value={`${String(ea.host ?? "127.0.0.1")}:${String(ea.port ?? 8765)}`} />
                <div className="mt-2 flex items-center justify-between">
                  <span className="label">EA live permission</span>
                  <Badge tone={ea.allow_live ? "bad" : "ok"}>{ea.allow_live ? "ALLOWED" : "PAPER"}</Badge>
                </div>
                <div className="mt-2 flex items-center justify-between">
                  <span className="label">EA token</span>
                  <Badge tone={ea.token_present ? "ok" : "warn"}>{ea.token_present ? "present" : "absent"}</Badge>
                </div>
              </div>
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="Model providers" eyebrow="Availability · no keys shown" action={<KeyRound className="h-4 w-4 text-mist-600" aria-hidden />} />
            <div className="space-y-3 p-4">
              <div className="flex items-center justify-between">
                <span className="label">LLM key</span>
                <Badge tone={llm.api_key_present ? "ok" : "warn"}>{llm.api_key_present ? "present" : "absent"}</Badge>
              </div>
              <div className="flex items-center justify-between">
                <span className="label">Kronos</span>
                <Badge tone={kronos.enabled ? "ok" : "idle"}>{kronos.enabled ? "enabled" : "off"}</Badge>
              </div>
              <div className="flex items-center justify-between">
                <span className="label">News filter</span>
                <Badge tone={news.enabled ? "ok" : "idle"}>{news.enabled ? "enabled" : "off"}</Badge>
              </div>
              <div className="flex items-center justify-between">
                <span className="label">Telegram</span>
                <Badge tone={telegram.bot_token_present ? (telegram.enabled ? "ok" : "idle") : "warn"}>
                  {telegram.bot_token_present ? (telegram.enabled ? "on" : "off") : "no token"}
                </Badge>
              </div>
              <p className="text-[10px] leading-relaxed text-mist-600">
                The system keeps running without an LLM. Missing providers degrade the committee, never the risk
                governor.
              </p>
            </div>
          </Panel>

          <Panel>
            <PanelHeader title="Runtime" eyebrow="This process" />
            <div className="space-y-2.5 p-4">
              {systemQuery.isLoading ? (
                <LoadingBlock label="Reading runtime info" />
              ) : systemQuery.isError ? (
                <ErrorNote title="Runtime info unavailable" />
              ) : (
                <>
                  <KeyValue label="Python" value={systemQuery.data?.python ?? "—"} />
                  <KeyValue label="Config" value={systemQuery.data?.config_path ?? "—"} />
                  <KeyValue label="Dashboard DB" value={systemQuery.data?.database ?? "—"} />
                  <KeyValue label="Token file" value={systemQuery.data?.token_file ?? "—"} />
                  <KeyValue
                    label="WS interval"
                    value={systemQuery.data ? `${num(systemQuery.data.ws_interval, 1)}s` : "—"}
                  />
                  <KeyValue
                    label="Server time"
                    value={systemQuery.data ? localTime(systemQuery.data.time) : "—"}
                  />
                </>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
