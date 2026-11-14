import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import type { Snapshot } from "../lib/types";

export type LiveStatus = "connecting" | "open" | "reconnecting" | "closed" | "unauthorized";

interface LiveContextValue {
  status: LiveStatus;
  snapshot: Snapshot | null;
  lastUpdate: number | null;
}

const LiveContext = createContext<LiveContextValue>({
  status: "closed",
  snapshot: null,
  lastUpdate: null,
});

function socketUrl(): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/v1/ws`;
}

export function LiveProvider({ token, children }: { token: string | null; children: ReactNode }) {
  const [status, setStatus] = useState<LiveStatus>("connecting");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [lastUpdate, setLastUpdate] = useState<number | null>(null);
  const attempts = useRef(0);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const pingTimer = useRef<number | null>(null);
  const disposed = useRef(false);

  useEffect(() => {
    if (!token) {
      setStatus("closed");
      return;
    }
    disposed.current = false;

    const connect = () => {
      if (disposed.current) return;
      setStatus(attempts.current === 0 ? "connecting" : "reconnecting");
      let socket: WebSocket;
      try {
        socket = new WebSocket(socketUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attempts.current = 0;
        socket.send(JSON.stringify({ type: "auth", token }));
        pingTimer.current = window.setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ type: "ping", ts: Date.now() }));
          }
        }, 20_000);
      };

      socket.onmessage = (event) => {
        try {
          const message = JSON.parse(String(event.data)) as { type?: string };
          if (message.type === "hello") {
            setStatus("open");
          } else if (message.type === "snapshot") {
            setSnapshot(message as unknown as Snapshot);
            setLastUpdate(Date.now());
            setStatus("open");
          }
        } catch {
          // Ignore malformed frames; the next snapshot is authoritative.
        }
      };

      socket.onclose = (event) => {
        if (pingTimer.current) window.clearInterval(pingTimer.current);
        pingTimer.current = null;
        socketRef.current = null;
        if (disposed.current) return;
        if (event.code === 4401) {
          setStatus("unauthorized");
          return;
        }
        scheduleReconnect();
      };

      socket.onerror = () => {
        if (socket.readyState !== WebSocket.CLOSED) socket.close();
      };
    };

    const scheduleReconnect = () => {
      if (disposed.current) return;
      attempts.current += 1;
      setStatus("reconnecting");
      const delay = Math.min(10_000, 800 * 2 ** Math.min(attempts.current, 4));
      reconnectTimer.current = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      disposed.current = true;
      if (reconnectTimer.current) window.clearTimeout(reconnectTimer.current);
      if (pingTimer.current) window.clearInterval(pingTimer.current);
      reconnectTimer.current = null;
      pingTimer.current = null;
      socketRef.current?.close(1000, "dashboard unmount");
      socketRef.current = null;
    };
  }, [token]);

  const value = useMemo(() => ({ status, snapshot, lastUpdate }), [status, snapshot, lastUpdate]);
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext);
}
