import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Square } from "lucide-react";

import { api, ApiError } from "../lib/api";
import type { RunnerStatus } from "../lib/types";
import { localTime } from "../lib/format";
import { Badge, KeyValue, Panel, PanelHeader, StatusDot } from "./Panel";
import { useToast } from "./Toast";

export function RunnerControl({ runner }: { runner?: RunnerStatus }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const statusQuery = useQuery({
    queryKey: ["runner"],
    queryFn: api.runner,
    refetchInterval: 10_000,
  });

  const current = statusQuery.data?.runner ?? runner;
  const running = Boolean(current?.pid) && current?.mode !== "stopped";
  const mode = current?.mode ?? "stopped";

  const start = useMutation({
    mutationFn: api.startRunner,
    onSuccess: (payload) => {
      toast.push("success", "DRY-RUN runner started", `PID ${payload.runner.pid ?? "?"} · no real orders`);
      void queryClient.invalidateQueries({ queryKey: ["runner"] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      void queryClient.invalidateQueries({ queryKey: ["risk"] });
    },
    onError: (error) => {
      toast.push(
        "error",
        "Runner did not start",
        error instanceof ApiError ? error.message : String(error),
      );
    },
  });

  const stop = useMutation({
    mutationFn: api.stopRunner,
    onSuccess: (payload) => {
      toast.push("success", "Runner stopped", payload.runner.stopped_pid ? `PID ${payload.runner.stopped_pid} terminated` : undefined);
      void queryClient.invalidateQueries({ queryKey: ["runner"] });
      void queryClient.invalidateQueries({ queryKey: ["overview"] });
      void queryClient.invalidateQueries({ queryKey: ["risk"] });
    },
    onError: (error) => {
      toast.push("error", "Stop failed", error instanceof ApiError ? error.message : String(error));
    },
  });

  return (
    <Panel>
      <PanelHeader
        title="Runner"
        eyebrow="DRY-RUN only"
        action={<Badge tone={running ? "gold" : "idle"}>{mode}</Badge>}
      />
      <div className="space-y-3 p-4">
        <div className="flex items-center gap-2">
          <StatusDot tone={running ? "ok" : "idle"} />
          <p className="text-[11px] text-mist-500">
            {running ? "A runner owns the pid file." : "No runner process is registered."}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <KeyValue label="PID" value={current?.pid ?? "—"} />
          <KeyValue label="Started" value={current?.started ? localTime(current.started) : "—"} />
        </div>

        <p className="text-[10px] leading-relaxed text-mist-600">
          The dashboard can only start <span className="text-mist-300">dry-run</span>. Live starts return 403 until
          the walk-forward gate passes and an engine is promoted.
        </p>

        <div className="flex gap-2">
          <button
            type="button"
            className="btn btn-primary flex-1"
            disabled={running || start.isPending}
            onClick={() => start.mutate()}
          >
            <Play className="h-3.5 w-3.5" aria-hidden />
            {start.isPending ? "Starting…" : "Start dry-run"}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            disabled={!running || stop.isPending}
            onClick={() => stop.mutate()}
          >
            <Square className="h-3.5 w-3.5" aria-hidden />
            {stop.isPending ? "Stopping…" : "Stop"}
          </button>
        </div>

        {current?.stale ? (
          <p className="text-[10px] text-signal-warn">A stale pid file was found and cleaned up.</p>
        ) : null}
      </div>
    </Panel>
  );
}
