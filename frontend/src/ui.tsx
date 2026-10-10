import { createContext, useCallback, useContext, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Info, Loader2, WifiOff, X, XCircle } from "lucide-react";

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

// ---------------------------------------------------------------- status colours (always paired with a label)

export type Tone = "ok" | "warn" | "bad" | "idle" | "accent";
const TONE: Record<Tone, string> = {
  ok: "text-ok border-ok/40 bg-ok/10",
  warn: "text-warn border-warn/40 bg-warn/10",
  bad: "text-bad border-bad/40 bg-bad/10",
  idle: "text-ink-2 border-line-strong bg-surface-2",
  accent: "text-accent border-accent/40 bg-accent/10",
};
const DOT: Record<Tone, string> = { ok: "bg-ok", warn: "bg-warn", bad: "bg-bad", idle: "bg-idle", accent: "bg-accent" };

export function Badge({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium whitespace-nowrap", TONE[tone], className)}>
      <span className={cx("h-1.5 w-1.5 rounded-full", DOT[tone])} />
      {children}
    </span>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  // Default size only when the caller sets none (two text-size classes would conflict).
  const sized = /(^|\s)text-(\[\d|xs|sm|base|lg|\d?xl)/.test(className ?? "");
  return <span className={cx("font-mono", !sized && "text-[12.5px]", className)}>{children}</span>;
}

// ---------------------------------------------------------------- card with loading / empty / error states

export function Card({ title, icon, actions, children, className, bodyClass }: {
  title?: ReactNode; icon?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string;
}) {
  return (
    <section className={cx("rounded-xl border border-line bg-surface", className)}>
      {title && (
        <header className="flex items-center justify-between gap-4 border-b border-line px-4 py-3">
          <h2 className="flex items-center gap-2 text-[13px] font-semibold text-ink">
            {icon && <span className="text-ink-2">{icon}</span>}
            {title}
          </h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx("p-4", bodyClass)}>{children}</div>
    </section>
  );
}

/** Renders loading / error / empty states, otherwise children. */
export function DataState({ loading, error, empty, emptyText = "No data", children, hasData }: {
  loading: boolean; error: string | null; empty?: boolean; emptyText?: ReactNode; children: ReactNode; hasData?: boolean;
}) {
  if (loading && !hasData) return <StateLine icon={<Loader2 className="h-4 w-4 animate-spin" />} text="Loading…" />;
  if (error && !hasData)
    return <StateLine icon={<WifiOff className="h-4 w-4 text-bad" />} text={error === "Backend unreachable" ? "Backend unreachable" : `Error: ${error}`} />;
  if (empty) return <StateLine icon={<Info className="h-4 w-4" />} text={emptyText} />;
  return (
    <>
      {error && hasData && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
          <AlertTriangle className="h-3.5 w-3.5" /> Showing last received data. Latest update failed: {error}
        </div>
      )}
      {children}
    </>
  );
}

export function StateLine({ icon, text }: { icon: ReactNode; text: ReactNode }) {
  return <div className="flex items-center gap-2 py-6 text-sm text-ink-3 justify-center">{icon}{text}</div>;
}

// ---------------------------------------------------------------- buttons

type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANT: Record<Variant, string> = {
  primary: "bg-accent-strong text-[#04110f] border-accent-strong hover:bg-accent",
  secondary: "bg-surface-2 text-ink border-line-strong hover:border-ink-3",
  danger: "bg-bad/90 text-white border-bad hover:bg-bad",
  ghost: "bg-transparent text-ink-2 border-transparent hover:text-ink hover:bg-surface-2",
};

export function Button({ variant = "secondary", busy, icon, children, className, ...rest }:
  ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean; icon?: ReactNode }) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || busy}
      className={cx("inline-flex h-9 items-center justify-center gap-2 rounded-lg border px-3.5 text-[13px] font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-45", VARIANT[variant], className)}
    >
      {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : icon}
      {children}
    </button>
  );
}

// ---------------------------------------------------------------- confirmation dialog

export function ConfirmDialog({ open, title, body, confirmLabel, tone = "danger", onConfirm, onCancel }: {
  open: boolean; title: string; body: ReactNode; confirmLabel: string; tone?: "danger" | "primary";
  onConfirm: () => void; onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div className="w-full max-w-md rounded-xl border border-line-strong bg-surface p-5 shadow-2xl" onClick={e => e.stopPropagation()}>
        <h3 className="text-base font-semibold">{title}</h3>
        <div className="mt-2 text-sm text-ink-2 leading-relaxed">{body}</div>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button variant={tone} onClick={onConfirm} autoFocus>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- toasts

type ToastKind = "ok" | "err" | "info";
interface Toast { id: number; kind: ToastKind; text: string }
const ToastCtx = createContext<(text: string, kind?: ToastKind) => void>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts(t => [...t, { id, kind, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), kind === "err" ? 9000 : 5000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-[60] flex w-[380px] flex-col gap-2">
        {toasts.map(t => (
          <div key={t.id} className={cx("flex items-start gap-2.5 rounded-lg border bg-surface-2 px-3.5 py-3 text-[13px] shadow-xl",
            t.kind === "ok" ? "border-ok/40" : t.kind === "err" ? "border-bad/50" : "border-line-strong")}>
            {t.kind === "ok" ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-ok" />
              : t.kind === "err" ? <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-bad" />
                : <Info className="mt-0.5 h-4 w-4 shrink-0 text-accent" />}
            <span className="flex-1 leading-snug">{t.text}</span>
            <button className="text-ink-3 hover:text-ink" onClick={() => setToasts(x => x.filter(y => y.id !== t.id))}><X className="h-3.5 w-3.5" /></button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
export const useToast = () => useContext(ToastCtx);

// ---------------------------------------------------------------- formatting

export const pct = (x: number | null | undefined, digits = 1) => (x == null ? "—" : `${(100 * x).toFixed(digits)}%`);
export const ms = (x: number | null | undefined) => (x == null ? "—" : `${Math.round(x)} ms`);
export const clock = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "—");
export const mmss = (seconds: number) => {
  const s = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
