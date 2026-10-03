"use client";

import Link from "next/link";
import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { errorMessage } from "@/lib/api";
import { AlertIcon, CheckIcon, InfoIcon } from "./icons";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const variants: Record<Variant, string> = {
  primary:
    "bg-lagoon text-white shadow-[0_1px_0_rgb(255_255_255/0.18)_inset,0_1px_2px_rgb(11_122_140/0.35)] hover:bg-lagoon-ink disabled:bg-faint disabled:shadow-none",
  secondary: "bg-paper text-ink border border-rule hover:border-[#c6cede] hover:bg-[#fbfcfe] disabled:text-faint",
  danger: "bg-paper text-unsafe-ink border border-unsafe/30 hover:bg-unsafe-tint hover:border-unsafe/50 disabled:text-faint",
  ghost: "text-lagoon-ink hover:bg-lagoon-tint disabled:text-faint",
};

export function Button({
  variant = "secondary",
  className = "",
  busy,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      disabled={props.disabled || busy}
      className={`pressable inline-flex items-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-[13.5px] font-semibold disabled:cursor-not-allowed ${variants[variant]} ${className}`}
    >
      {busy ? <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-r-transparent" aria-hidden /> : null}
      {children}
    </button>
  );
}

export function LinkButton({ href, variant = "secondary", children }: { href: string; variant?: Variant; children: ReactNode }) {
  return (
    <Link href={href} className={`pressable inline-flex items-center gap-2 whitespace-nowrap rounded-lg px-3.5 py-2 text-[13.5px] font-semibold ${variants[variant]}`}>
      {children}
    </Link>
  );
}

const field =
  "w-full rounded-lg border border-rule bg-paper px-3 py-2 text-[14px] text-ink placeholder:text-faint transition-[border-color,box-shadow] duration-150 hover:border-[#c9d1e0] focus:border-lagoon-bright focus:outline-none focus:ring-4 focus:ring-lagoon-bright/15";

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={`${field} ${props.className ?? ""}`} />;
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={`${field} font-mono text-[12.5px] leading-relaxed ${props.className ?? ""}`} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={`${field} pr-8 ${props.className ?? ""}`} />;
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={htmlFor} className="block text-[13px] font-semibold text-ink">
        {label}
      </label>
      {children}
      {hint ? <p className="text-[12.5px] leading-snug text-slate">{hint}</p> : null}
    </div>
  );
}

export function PageHeader({ title, description, actions, crumb }: { title: ReactNode; description?: ReactNode; actions?: ReactNode; crumb?: ReactNode }) {
  return (
    <header className="mb-8">
      {crumb ? <div className="mb-2 text-[13px]">{crumb}</div> : null}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-[26px] font-bold leading-tight tracking-[-0.02em]">{title}</h1>
          {description ? <p className="mt-1.5 max-w-[70ch] text-[14.5px] text-slate">{description}</p> : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
    </header>
  );
}

export function Crumb({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className="inline-flex items-center gap-1 font-medium text-lagoon-ink hover:underline">
      <span aria-hidden>‹</span> {children}
    </Link>
  );
}

export function Section({ title, description, actions, children, id }: { title: string; description?: ReactNode; actions?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section id={id} className="mb-10">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-[16px] font-bold tracking-[-0.01em]">{title}</h2>
          {description ? <p className="mt-0.5 max-w-[74ch] text-[13px] text-slate">{description}</p> : null}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-xl border border-rule bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04)] ${className}`}>{children}</div>;
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="overflow-x-auto rounded-xl border border-rule bg-paper shadow-[0_1px_2px_rgb(22_32_58/0.04)]">{children}</div>;
}

export function EmptyState({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-[#cdd5e3] bg-paper/70 px-6 py-12 text-center">
      {icon ? <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-full bg-lagoon-tint text-lagoon">{icon}</div> : null}
      <p className="text-[15px] font-semibold">{title}</p>
      {children ? <div className="mx-auto mt-1 max-w-[58ch] text-slate">{children}</div> : null}
      {action ? <div className="mt-5 flex justify-center">{action}</div> : null}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex animate-fade items-start gap-2 rounded-lg border border-unsafe/25 bg-unsafe-tint px-3 py-2.5 text-[13px] text-unsafe-ink">
      <AlertIcon size={16} className="mt-0.5 shrink-0" />
      <span>{errorMessage(error)}</span>
    </div>
  );
}

export function Notice({ tone = "warn", children, title }: { tone?: "warn" | "info" | "danger"; children: ReactNode; title?: string }) {
  const styles = {
    warn: { box: "border-unsure-bright/40 bg-unsure-tint text-[#6b4204]", icon: <AlertIcon size={17} className="mt-px shrink-0 text-unsure" /> },
    info: { box: "border-lagoon-bright/30 bg-lagoon-tint text-[#0b4e59]", icon: <InfoIcon size={17} className="mt-px shrink-0 text-lagoon" /> },
    danger: { box: "border-unsafe/30 bg-unsafe-tint text-unsafe-ink", icon: <AlertIcon size={17} className="mt-px shrink-0 text-unsafe" /> },
  }[tone];
  return (
    <div className={`flex items-start gap-2.5 rounded-lg border px-3.5 py-2.5 text-[13.5px] leading-snug ${styles.box}`}>
      {styles.icon}
      <div>
        {title ? <div className="font-semibold">{title}</div> : null}
        {children}
      </div>
    </div>
  );
}

/** Placeholder rows that shimmer while data loads. */
export function Loading({ rows = 4 }: { rows?: number; label?: string }) {
  return (
    <div className="space-y-3 py-2" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading</span>
      <div className="skeleton h-5 w-1/3" />
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-11" style={{ opacity: 1 - i * 0.15 }} />
      ))}
    </div>
  );
}

const statusTones: Record<string, string> = {
  ok: "bg-safe-tint text-safe-ink",
  completed: "bg-safe-tint text-safe-ink",
  ready: "bg-safe-tint text-safe-ink",
  good: "bg-safe-tint text-safe-ink",
  error: "bg-unsafe-tint text-unsafe-ink",
  failed: "bg-unsafe-tint text-unsafe-ink",
  poor: "bg-unsafe-tint text-unsafe-ink",
  aborted_budget: "bg-unsafe-tint text-unsafe-ink",
  diverged: "bg-unsure-tint text-unsure-ink",
  moderate: "bg-unsure-tint text-unsure-ink",
  insufficient_data: "bg-unsure-tint text-unsure-ink",
  uncalibrated: "bg-unsure-tint text-unsure-ink",
  running: "bg-lagoon-tint text-lagoon-ink",
  building: "bg-lagoon-tint text-lagoon-ink",
  queued: "bg-mist text-slate",
  cancelled: "bg-mist text-slate",
  skipped: "bg-mist text-slate",
};

const statusLabels: Record<string, string> = {
  aborted_budget: "stopped: budget",
  insufficient_data: "not enough evidence",
};

export function Status({ value }: { value: string }) {
  const live = value === "running" || value === "building";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[12.5px] font-semibold ${statusTones[value] ?? "bg-mist text-slate"}`}>
      <span className={`h-1.5 w-1.5 rounded-full bg-current ${live ? "live-dot" : ""}`} aria-hidden />
      {statusLabels[value] ?? value.replaceAll("_", " ")}
    </span>
  );
}

export function Tag({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "lagoon" }) {
  const cls = tone === "lagoon" ? "bg-lagoon-tint text-lagoon-ink" : "bg-mist text-slate";
  return <span className={`inline-block rounded-md px-1.5 py-px text-[12px] font-medium ${cls}`}>{children}</span>;
}

export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono text-[12.5px] ${className}`}>{children}</span>;
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2 text-[13.5px]">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-slate">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** Segmented tabs with an indicator that slides to the selected option. */
export function Tabs<T extends string>({ value, options, onChange, label }: { value: T; options: readonly (readonly [T, string])[]; onChange: (v: T) => void; label: string }) {
  const index = Math.max(0, options.findIndex(([k]) => k === value));
  return (
    <div role="tablist" aria-label={label} className="relative inline-grid rounded-lg bg-mist p-1" style={{ gridTemplateColumns: `repeat(${options.length}, minmax(0, 1fr))` }}>
      <span
        aria-hidden
        className="absolute bottom-1 top-1 rounded-md bg-paper shadow-[0_1px_3px_rgb(22_32_58/0.12)] transition-transform duration-300 ease-[var(--ease-out-soft)]"
        style={{ width: `calc((100% - 0.5rem) / ${options.length})`, left: "0.25rem", transform: `translateX(${index * 100}%)` }}
      />
      {options.map(([k, text]) => (
        <button
          key={k}
          type="button"
          role="tab"
          aria-selected={value === k}
          onClick={() => onChange(k)}
          className={`relative z-10 whitespace-nowrap rounded-md px-3.5 py-1.5 text-[13px] font-semibold transition-colors ${value === k ? "text-ink" : "text-slate hover:text-ink"}`}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

// --- Toasts ---------------------------------------------------------------------------

type Toast = { id: number; text: string; tone: "ok" | "error" };
const ToastContext = createContext<(text: string, tone?: "ok" | "error") => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-2), { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3200);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-50 flex flex-col items-end gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`toast-in pointer-events-auto flex items-center gap-2 rounded-lg px-3.5 py-2.5 text-[13.5px] font-medium text-white shadow-[0_8px_24px_rgb(17_28_51/0.25)] ${t.tone === "ok" ? "bg-rail" : "bg-unsafe-ink"}`}
          >
            {t.tone === "ok" ? <CheckIcon size={16} className="text-lagoon-bright" /> : <AlertIcon size={16} />}
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
