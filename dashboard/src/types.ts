/** Mirrors the response models in hangar/api.py. */

export type AppStatus =
  | "queued"
  | "building"
  | "running"
  /** Stopped to save memory after a spell with no traffic, woken by a visit. */
  | "sleeping"
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

/** A secret's existence. There is deliberately no `value` — the API never
 *  returns one, to anybody. */
export interface Secret {
  name: string;
  created_at: string;
  updated_at: string;
}

export interface Sample {
  /** Unix seconds. */
  at: number;
  /** Percent of one core, so 200 means two cores saturated. */
  cpu_percent: number;
  memory_mb: number;
  memory_limit_mb: number;
  memory_percent: number;
}

export interface Metrics {
  app_id: string;
  /** Seconds between samples. */
  interval: number;
  window_minutes: number;
  memory_limit_mb: number;
  cpu_limit: number;
  current: Sample | null;
  samples: Sample[];
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
  /** Seconds of no traffic before an app sleeps; 0 means apps stay up. */
  idle_timeout: number;
  idle_reaper: boolean;
  /** Whether the resource sampler is running. */
  metrics: boolean;
}

/** Statuses that can still change on their own, so the UI keeps polling. */
export const IN_FLIGHT: AppStatus[] = ["queued", "building"];

export type Role = "owner" | "editor" | "viewer";

export interface WhoAmI {
  kind: string;
  email: string | null;
  is_admin: boolean;
  authenticated: boolean;
}

export interface Grant {
  user_id: string;
  email: string;
  role: Role;
  can: string[];
  granted_at: string;
}

export interface User {
  id: string;
  email: string;
  is_admin: boolean;
  is_active: boolean;
  created_at: string;
}

export interface Invite {
  user: User;
  /** One-time. Hangar sends no email, so this is handed over directly. */
  invite_token: string;
}

export const ROLES: Role[] = ["owner", "editor", "viewer"];

/** What each role may do, mirroring hangar/permissions.py for the UI copy. */
export const ROLE_SUMMARY: Record<Role, string> = {
  owner: "Full control, including sharing and deleting",
  editor: "Can deploy, restart, and read logs",
  viewer: "Can see the app — not its logs",
};
