import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, TriangleAlert, X } from "lucide-react";

type Tone = "success" | "error" | "info";

interface Toast {
  id: number;
  tone: Tone;
  title: string;
  detail?: string;
}

interface ToastContextValue {
  push: (tone: Tone, title: string, detail?: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const nextId = useRef(1);

  const remove = useCallback((id: number) => {
    setItems((current) => current.filter((item) => item.id !== id));
  }, []);

  const push = useCallback(
    (tone: Tone, title: string, detail?: string) => {
      const id = nextId.current++;
      setItems((current) => [...current.slice(-3), { id, tone, title, detail }]);
      window.setTimeout(() => remove(id), tone === "error" ? 9000 : 5500);
    },
    [remove],
  );

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(360px,calc(100vw-2rem))] flex-col gap-2">
        {items.map((item) => (
          <div
            key={item.id}
            role={item.tone === "error" ? "alert" : "status"}
            className={`pointer-events-auto animate-rise border bg-ink-900/95 p-3 shadow-panel backdrop-blur ${
              item.tone === "error"
                ? "border-signal-down/45"
                : item.tone === "success"
                  ? "border-signal-up/40"
                  : "border-ink-600"
            }`}
          >
            <div className="flex items-start gap-2.5">
              {item.tone === "error" ? (
                <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-signal-down" aria-hidden />
              ) : (
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-signal-up" aria-hidden />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-mist-100">{item.title}</p>
                {item.detail ? <p className="mt-1 break-words text-xs text-mist-500">{item.detail}</p> : null}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                className="text-mist-500 hover:text-mist-100"
                onClick={() => remove(item.id)}
              >
                <X className="h-4 w-4" aria-hidden />
              </button>
            </div>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used inside ToastProvider");
  return context;
}
