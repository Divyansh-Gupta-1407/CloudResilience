import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { get, type Errors, type Metrics, type ServiceRow, type Squad, type Status } from "./api";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loading: boolean;       // true until the first response (or failure)
  refresh: () => void;
}

/** Poll a GET endpoint. Keeps the last good data when a later poll fails, and reports the failure. */
export function usePoll<T>(path: string | null, intervalMs = 2000): Polled<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const busy = useRef(false);

  const tick = useCallback(async () => {
    if (!path || busy.current) return;
    busy.current = true;
    try {
      setData(await get<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      busy.current = false;
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    setLoading(true);
    tick();
    const id = setInterval(tick, intervalMs);
    return () => clearInterval(id);
  }, [tick, intervalMs]);

  return { data, error, loading, refresh: tick };
}

// ---------------------------------------------------------------- live data shared by every page

interface Live {
  status: Polled<Status>;
  metrics: Polled<Metrics>;
  errors: Polled<Errors>;
  squad: Polled<Squad>;
  services: Polled<ServiceRow[]>;
  refreshAll: () => void;
}

const LiveContext = createContext<Live | null>(null);

export function LiveProvider({ children }: { children: ReactNode }) {
  const status = usePoll<Status>("/status");
  const metrics = usePoll<Metrics>("/metrics?limit=120");
  const errors = usePoll<Errors>("/errors?limit=200");
  const squad = usePoll<Squad>("/squad");
  const services = usePoll<ServiceRow[]>("/services", 4000);
  const refreshAll = useCallback(() => {
    status.refresh(); metrics.refresh(); errors.refresh(); squad.refresh(); services.refresh();
  }, [status.refresh, metrics.refresh, errors.refresh, squad.refresh, services.refresh]);
  return (
    <LiveContext.Provider value={{ status, metrics, errors, squad, services, refreshAll }}>
      {children}
    </LiveContext.Provider>
  );
}

export function useLive(): Live {
  const v = useContext(LiveContext);
  if (!v) throw new Error("useLive outside LiveProvider");
  return v;
}

/** Re-render every second (for "active for mm:ss" timers). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}
