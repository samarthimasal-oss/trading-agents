import { useEffect, useRef, useState } from "react";
import { TriangleAlert, X } from "lucide-react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  tone?: "danger" | "normal";
  requireText?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  tone = "normal",
  requireText,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setTyped("");
      return;
    }
    const timer = window.setTimeout(() => inputRef.current?.focus(), 30);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open, busy, onCancel]);

  if (!open) return null;

  const confirmed = !requireText || typed.trim() === requireText;

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-ink-950/80 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-description"
        className="w-full max-w-md border border-ink-600 bg-ink-900 shadow-panel"
      >
        <div className="flex items-start justify-between border-b border-ink-700 px-4 py-3">
          <div className="flex items-start gap-2.5">
            {tone === "danger" ? (
              <TriangleAlert className="mt-0.5 h-4 w-4 text-signal-down" aria-hidden />
            ) : null}
            <h2 id="confirm-title" className="font-display text-sm font-semibold uppercase tracking-[0.12em]">
              {title}
            </h2>
          </div>
          <button
            type="button"
            aria-label="Close dialog"
            className="text-mist-500 hover:text-mist-100"
            onClick={onCancel}
            disabled={busy}
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <div className="space-y-4 px-4 py-4">
          <p id="confirm-description" className="text-sm leading-relaxed text-mist-300">
            {description}
          </p>
          {requireText ? (
            <label className="block">
              <span className="label">
                Type <span className="text-gold-300">{requireText}</span> to continue
              </span>
              <input
                ref={inputRef}
                className="input mt-2"
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                disabled={busy}
              />
            </label>
          ) : null}
        </div>
        <div className="flex justify-end gap-2 border-t border-ink-700 px-4 py-3">
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${tone === "danger" ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
            disabled={busy || !confirmed}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
