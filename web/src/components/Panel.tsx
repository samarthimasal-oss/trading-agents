import type { ReactNode } from "react";

export function Panel({
  children,
  className = "",
  ...rest
}: {
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section className={`panel ${className}`} {...rest}>
      {children}
    </section>
  );
}

export function PanelHeader({
  title,
  eyebrow,
  action,
  danger = false,
}: {
  title: string;
  eyebrow?: string;
  action?: ReactNode;
  danger?: boolean;
}) {
  return (
    <header className="panel-header">
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow mb-1">{eyebrow}</p> : null}
        <h2 className={`panel-title ${danger ? "text-signal-down" : ""}`}>{title}</h2>
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </header>
  );
}

export function StatusDot({ tone }: { tone: "ok" | "warn" | "bad" | "idle" | "gold" }) {
  const color = {
    ok: "bg-signal-up",
    warn: "bg-signal-warn",
    bad: "bg-signal-down",
    idle: "bg-mist-600",
    gold: "bg-gold-400",
  }[tone];
  return <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${color}`} aria-hidden />;
}

export function Badge({
  tone = "idle",
  children,
}: {
  tone?: "ok" | "warn" | "bad" | "idle" | "gold";
  children: ReactNode;
}) {
  const className = {
    ok: "badge badge-ok",
    warn: "badge badge-warn",
    bad: "badge badge-bad",
    idle: "badge badge-idle",
    gold: "badge badge-gold",
  }[tone];
  return <span className={className}>{children}</span>;
}

export function EmptyState({
  title,
  detail,
  icon,
}: {
  title: string;
  detail?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      {icon ? <div className="text-mist-600">{icon}</div> : null}
      <p className="font-display text-xs font-semibold uppercase tracking-[0.18em] text-mist-500">{title}</p>
      {detail ? <p className="max-w-md text-xs leading-relaxed text-mist-600">{detail}</p> : null}
    </div>
  );
}

export function ErrorNote({ title, detail }: { title: string; detail?: string }) {
  return (
    <div role="alert" className="border border-signal-down/35 bg-signal-down/5 px-3 py-2.5">
      <p className="text-sm font-medium text-signal-down">{title}</p>
      {detail ? <p className="mt-1 break-words text-xs text-mist-500">{detail}</p> : null}
    </div>
  );
}

export function LoadingBlock({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 px-4 py-8 text-xs text-mist-500" role="status">
      <span className="h-1.5 w-1.5 animate-pulseSoft rounded-full bg-gold-400" />
      {label}
    </div>
  );
}

export function KeyValue({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return (
    <div className="min-w-0">
      <p className="label">{label}</p>
      <p className={`mt-1 truncate font-mono text-sm tabular-nums ${tone ?? "text-mist-100"}`}>{value}</p>
    </div>
  );
}
