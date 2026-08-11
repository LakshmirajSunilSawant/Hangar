/** Client for the Hangar control plane.
 *
 * The API token lives in localStorage. That is readable by any script on the
 * page, which is acceptable only because this dashboard is served by the
 * control plane itself and loads no third-party code. It would not be
 * acceptable once the PRD's per-user identity layer exists — that wants a
 * session cookie the page cannot read.
 */

import type { App, Health, Logs, Scan } from "./types";

const TOKEN_KEY = "hangar.token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }

  /** The token is missing or wrong, as opposed to the request being bad. */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    // fetch only rejects on network failure, which for a page served by the
    // control plane almost always means the control plane stopped.
    throw new ApiError(0, "could not reach the control plane — is it running?");
  }

  if (response.status === 204) return undefined as T;

  const body = await response.text();
  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(body, response.status));
  }
  return body ? (JSON.parse(body) as T) : (undefined as T);
}

function extractDetail(body: string, status: number): string {
  try {
    const parsed = JSON.parse(body);
    if (typeof parsed.detail === "string") return parsed.detail;
    // FastAPI validation errors arrive as a list of objects.
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((d: { loc?: string[]; msg?: string }) =>
          [d.loc?.slice(1).join("."), d.msg].filter(Boolean).join(": "),
        )
        .join("; ");
    }
  } catch {
    /* fall through to the raw body */
  }
  return body.trim() || `request failed with status ${status}`;
}

export const api = {
  health: () => request<Health>("/healthz"),

  list: () => request<App[]>("/apps"),

  get: (id: string) => request<App>(`/apps/${id}`),

  logs: (id: string) => request<Logs>(`/apps/${id}/logs`),

  scan: (id: string) => request<Scan>(`/apps/${id}/scan`),

  fromPath: (name: string, sourcePath: string) =>
    request<App>("/apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, source_path: sourcePath }),
    }),

  fromRepo: (name: string, repoUrl: string, ref?: string) =>
    request<App>("/apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, repo_url: repoUrl, ref: ref || null }),
    }),

  fromZip: (name: string, file: File) => {
    const form = new FormData();
    form.append("name", name);
    form.append("file", file);
    // No Content-Type header: the browser must set the multipart boundary.
    return request<App>("/apps/upload", { method: "POST", body: form });
  },

  redeploy: (id: string) => request<App>(`/apps/${id}/redeploy`, { method: "POST" }),
  stop: (id: string) => request<App>(`/apps/${id}/stop`, { method: "POST" }),
  restart: (id: string) => request<App>(`/apps/${id}/restart`, { method: "POST" }),
  remove: (id: string) => request<void>(`/apps/${id}`, { method: "DELETE" }),
};
