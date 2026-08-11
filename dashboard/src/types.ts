/** Mirrors the response models in hangar/api.py. */

export type AppStatus =
  | "queued"
  | "building"
  | "running"
  | "stopped"
  | "failed";

export type SourceType = "path" | "zip" | "repo";

export interface App {
  id: string;
  name: string;
  status: AppStatus;
  url: string | null;
  runtime: string | null;
  framework: string | null;
  source_type: SourceType;
  source_ref: string;
  source_revision: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface Logs {
  app_id: string;
  build_log: string;
  runtime_log: string;
}

export type Severity = "low" | "medium" | "high";

export interface Finding {
  tool: string;
  rule: string;
  severity: Severity;
  message: string;
  file: string;
  line: number;
}

export interface Scan {
  app_id: string;
  status: "skipped" | "clean" | "flagged" | "blocked";
  policy: "flag" | "block" | "off";
  counts: Record<Severity, number>;
  highest_severity: Severity | null;
  findings: Finding[];
  tools_run: string[];
  tools_skipped: Record<string, string>;
}

export interface Health {
  status: string;
  backend: string;
  backend_available: boolean;
  router: string;
  router_available: boolean;
  app_domain: string | null;
  auth: "enabled" | "disabled";
  sandbox_runtime: string;
}

/** Statuses that can still change on their own, so the UI keeps polling. */
export const IN_FLIGHT: AppStatus[] = ["queued", "building"];
