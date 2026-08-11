import type { AppStatus, Severity } from "../types";

type Tone = "ok" | "warn" | "bad" | "idle";

const APP_TONE: Record<AppStatus, Tone> = {
  running: "ok",
  building: "warn",
  queued: "idle",
  stopped: "idle",
  failed: "bad",
};

export function StatusChip({ status }: { status: AppStatus }) {
  const busy = status === "queued" || status === "building";
  return (
    <span className={`chip ${APP_TONE[status]}${busy ? " busy" : ""}`}>
      <span className="dot" aria-hidden="true" />
      {status}
    </span>
  );
}

const SEVERITY_TONE: Record<Severity, Tone> = {
  high: "bad",
  medium: "warn",
  low: "idle",
};

export function SeverityChip({ severity }: { severity: Severity }) {
  return <span className={`chip ${SEVERITY_TONE[severity]}`}>{severity}</span>;
}

export function ScanChip({ status }: { status: string }) {
  const tone: Tone =
    status === "clean" ? "ok"
    : status === "flagged" ? "warn"
    : status === "blocked" ? "bad"
    : "idle";
  return <span className={`chip ${tone}`}>scan: {status}</span>;
}

export function Chip({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`chip ${tone}`}>{children}</span>;
}
