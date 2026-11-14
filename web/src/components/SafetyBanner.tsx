import { Link } from "react-router-dom";
import { ShieldAlert } from "lucide-react";

export function SafetyBanner() {
  return (
    <div className="safety-stripe flex flex-col gap-2 border border-gold-500/30 bg-ink-900/70 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-2.5">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-gold-400" aria-hidden />
        <div>
          <p className="font-display text-[11px] font-semibold uppercase tracking-[0.16em] text-gold-300">
            Paper supervision only · no engine promoted
          </p>
          <p className="mt-0.5 text-xs text-mist-500">
            Walk-forward failed 7 of 9 gates on 2026-11-12. The native EA stays in PAPER; live starts are refused.
          </p>
        </div>
      </div>
      <Link className="link shrink-0 font-display text-[10px] uppercase tracking-[0.16em]" to="/backtests">
        Read the evidence
      </Link>
    </div>
  );
}
