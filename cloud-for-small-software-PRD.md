# PRD: Cloud for Small Software (working name: "Hangar")

**Status:** Draft v1
**Author:** Sakshi
**Constraint:** Every component in this stack must be free-to-use or open source. No paid SaaS dependencies in the MVP.

---

## 1. Problem Statement

Agents (Claude Code, Cursor, v0, etc.) have made it trivial to generate small, purpose-built software — internal dashboards, one-off tools, team utilities. But going from "generated code on my laptop" to "a live tool my team can use, safely, with the right people having access" is still hard. Existing clouds (AWS, Azure, GCP) were built for Big Software that scales to millions of users, and they carry that complexity (IAM, VPCs, billing dashboards) even for a tool three people will ever open.

**The gap:** there is no "share this like a Google Doc" experience for AI-generated software.

## 2. Goal

Build a platform where a generated app (repo or zip) becomes a live, permissioned, shareable URL in under 60 seconds, with no server, security, or auth knowledge required from the person deploying it.

## 3. Non-Goals (v1)

- Not competing with Vercel/Render for production-scale customer-facing apps.
- Not supporting every language/framework on day one — start with Python (FastAPI/Flask/Streamlit) and Node (Express/Next.js), since that's what most agent-generated tools use.
- Not building a general-purpose PaaS billing/marketplace layer yet.

## 4. Target User (v1 wedge)

A small team (3-15 people) at a startup that already uses Claude Code / Cursor to generate internal tools, and currently has no good way to deploy and share them beyond "runs on my laptop" or "I manually set up a VPS once and now I'm afraid to touch it."

## 5. Core User Flow

1. User points the platform at a repo/zip, or triggers deploy via a CLI/GitHub Action, or (v2) a coding-agent plugin calls a deploy API directly at the end of a session.
2. Platform detects app type (static analysis of entrypoint, dependencies).
3. Platform runs a security scan pass on the code before executing it.
4. Platform builds and deploys into an isolated sandbox, provisions a scoped database if needed, and returns a URL.
5. Owner sets permissions (owner / can-edit / can-view) and shares the link — recipients auth through the platform, not the app itself.
6. Owner can see logs, resource usage, and errors from a simple dashboard. One-click "stop/restart/delete."

## 6. Technical Architecture — 100% Free/OSS Stack

| Layer | Component | Tool | License/Cost |
|---|---|---|---|
| Sandboxed execution | Isolation runtime | **gVisor** (Google) or **Kata Containers** — user-space kernel isolation for untrusted code | Apache 2.0, free |
| Container runtime | Container engine | **Docker Engine / containerd** | Apache 2.0, free |
| Orchestration | Scheduling multiple app containers | **K3s** (lightweight Kubernetes) — self-hosted | Apache 2.0, free |
| Reverse proxy / TLS | Routing + auto HTTPS certs | **Caddy** or **Traefik** | Apache 2.0/MIT, free |
| Auth & permissions | Identity, login, scoped access | **Ory Kratos** (self-hosted identity server) or **Keycloak** | Apache 2.0, free |
| Database (per-app) | Scoped storage per deployed app | **PostgreSQL** (self-hosted) with per-tenant schemas, or **SQLite** for the smallest tools | PostgreSQL License / Public Domain, free |
| Object storage | File uploads, build artifacts | **MinIO** (S3-compatible, self-hosted) | AGPLv3, free |
| Build pipeline | CI to build + push images | **GitHub Actions** (free tier: 2,000 min/month on public repos, generous on private for small use) | Free tier |
| Static/security analysis | Scan untrusted code before execution | **Semgrep** (OSS rules) + **Bandit** (Python) + **npm audit** / **osv-scanner** | LGPL/MIT, free |
| Observability — metrics | Resource usage, uptime | **Prometheus** | Apache 2.0, free |
| Observability — dashboards | Owner-facing usage view | **Grafana** (OSS edition) | AGPLv3, free |
| Observability — logs | App logs surfaced to owner | **Loki** (Grafana Labs) | AGPLv3, free |
| Backend API | Platform control plane | **FastAPI** (Python) | MIT, free |
| Frontend | Owner dashboard, permission UI | **React** + **Vite** | MIT, free |
| Git integration | Pull source from repos | **GitHub REST API** | Free tier |
| Secrets management | Per-app env vars/secrets | **OpenBao** (Linux Foundation-governed fork of Vault, API-compatible) or simpler: encrypted at rest in Postgres via `libsodium` | MPL 2.0, free — *note: HashiCorp Vault itself moved off MPL 2.0 to the non-OSI Business Source License in 2023, so it no longer qualifies under this project's free/OSS rule. OpenBao is the correctly-licensed drop-in replacement.* |

## 6A. Zero-Cost, Always-Online Hosting Architecture

Your actual constraint is stricter than "free/OSS software" — the whole thing needs to run 24/7 and never generate a bill, indefinitely. That requires decisions beyond software licensing: compute, a domain, TLS, and uptime strategy. Here's the concrete $0 setup, verified against current terms (Oracle changed their free tier partway through 2026, so the commonly-cited "4 OCPU/24GB" figure is now out of date):

| Need | Free-forever solution | Notes |
|---|---|---|
| Compute | **Oracle Cloud Always Free — Ampere A1 (ARM)** | As of mid-2026, Oracle reduced this tier to **2 OCPUs / 12 GB RAM / 200 GB block storage**, down from the previously advertised 4 OCPU/24GB. Verify current limits at signup — this has changed once already and could again. This resource cut means the full observability stack (Prometheus+Grafana+Loki) alongside K3s+Postgres+MinIO+OpenBao+sandboxed app containers will be tight — see trimmed footprint below. |
| Domain name | **DuckDNS** (free dynamic DNS subdomain, e.g. `yourapp.duckdns.org`) | A real registered domain (`.com`) costs money every year, which breaks the $0 rule. DuckDNS gives you a stable hostname pointing at your Oracle VM's IP for free, indefinitely. |
| TLS/HTTPS | **Let's Encrypt**, auto-issued and renewed by **Caddy** | Free, automatic, renews itself — no cost, no manual renewal. Works fine against a DuckDNS hostname. |
| Preventing idle reclamation | **UptimeRobot** free tier (ping your service every 5 min) | Oracle can reclaim an Always Free Ampere instance if its 95th-percentile CPU stays under 20% for 7 straight days — a real risk for a low-traffic MVP with no users yet. A free UptimeRobot health-check ping doubles as uptime monitoring *and* keeps baseline activity high enough to avoid the idle flag. Once you have real design-partner traffic, this becomes unnecessary. |
| CI/CD | **GitHub Actions** free tier (2,000 min/month) | Free-to-use, not open source, but $0 cost within normal MVP usage. |
| Backups | **Restic** (open source) backing up to a second free-tier resource, e.g. Oracle's free Object Storage (20 GB) or a free-tier Backblaze B2 allotment | Needed regardless of hosting choice — losing the only VM without backups means losing everything. |

**Trimmed MVP footprint (fits 2 OCPU/12GB):** given the reduced Always Free specs, don't run the full observability stack from day one. Start with:
- K3s (lightweight, ~512MB overhead) + Docker/containerd
- PostgreSQL (single instance, modest shared_buffers)
- OpenBao for secrets
- Caddy for routing/TLS
- App sandboxes (gVisor/Kata) — this is where most of your RAM budget should go, since it's the actual product
- **Defer** Prometheus+Grafana+Loki and MinIO until either (a) you outgrow one box and add a second free Ampere instance (Oracle allows splitting the 2 OCPU/12GB across up to 4 small VMs, so a second always-free instance is possible at $0), or (b) you have real users and can justify the resource cost. Start with plain structured logs to local disk — good enough for an MVP owner-facing "view logs" feature.

This keeps the whole system genuinely $0/month indefinitely, not just $0 during a trial period.

## 7. Data Model (v1, simplified)

- **User**: id, email, auth_provider_id
- **App**: id, owner_id, name, source_type (repo/zip), runtime (python/node), status
- **Deployment**: id, app_id, image_ref, build_log_ref, status, created_at
- **Permission**: app_id, user_id, role (owner/editor/viewer)
- **AppDatabase**: app_id, db_type (postgres_schema/sqlite), connection_ref
- **ResourceUsage**: app_id, cpu_ms, memory_mb, request_count, timestamp (for Prometheus rollups)

## 8. Security Requirements (non-negotiable for production-grade)

- All generated code executes inside gVisor/Kata sandboxes — never directly on the host kernel.
- Every app gets hard resource caps (CPU, memory, execution time, egress bandwidth) enforced at the cgroup/sandbox level, so one runaway app can't degrade the platform.
- Static analysis (Semgrep/Bandit/osv-scanner) runs before first execution; flag (don't necessarily block in v1) suspicious patterns: raw network calls to unknown hosts, filesystem escapes, subprocess/eval usage.
- Network egress from sandboxes is default-deny except to an explicit allowlist (the app's declared dependencies' registries at build time; nothing at runtime unless declared).
- Auth is platform-level, not app-level — recipients authenticate to Ory Kratos/Keycloak before ever reaching the app's own routes, via a proxy layer that injects identity headers.
- Secrets are never stored in plaintext or in the generated code — injected at runtime from Vault.

## 9. Success Metrics (MVP)

- Time from "repo pushed" to "live URL": target < 60 seconds for a simple app.
- Cold start time on an idle app being reopened: target < 3 seconds (this is the biggest technical risk — validate with a spike before building the rest).
- Zero cross-tenant isolation breaches (measured via internal red-team testing, not just trust).
- 3-5 real design partner teams deploying at least one real internal tool within the first month.

## 10. Milestones

1. **Spike (week 1):** Provision the Oracle Always Free Ampere A1 instance (2 OCPU/12GB under current terms), set up DuckDNS + Caddy + Let's Encrypt, and prove cold-start time with gVisor/Kata on that box. This is the go/no-go gate — if cold starts can't get under a few seconds on this reduced hardware budget, the architecture needs to change (e.g., a second free Ampere instance, or a keep-warm pool sized to fit) before anything else is built.
2. **Core deploy pipeline (weeks 2-3):** repo/zip in → sandboxed container out → URL. No auth yet, no permissions — just "does the box work."
3. **Auth + permission layer (week 4):** Ory Kratos integration, owner/editor/viewer roles, proxy-level identity injection.
4. **Per-app database provisioning (week 5):** automatic Postgres schema or SQLite file per app, with backups.
5. **Observability dashboard (week 6):** Prometheus + Grafana + Loki wired to a simple owner-facing UI (logs, resource usage, restart/delete controls).
6. **Design partner onboarding (weeks 7-8):** 3-5 real teams, real feedback, fix the permission/sharing UX based on what they actually try to do (this is where most teams' assumptions break).

## 11. Risks

- **Free-tier resource ceiling**: Oracle's Always Free Ampere spec was cut roughly in half in 2026 (now 2 OCPU/12GB, not the often-cited 4/24). Design the MVP for this reduced budget from day one rather than assuming the larger, older figure — verify current limits before building, since this has changed once already.
- **Idle-reclamation risk**: with no real users yet, the instance can sit under Oracle's 20% CPU-utilization idle threshold and get reclaimed. Mitigate with a free UptimeRobot health-check ping until you have genuine design-partner traffic.
- **Cold start latency** is the single biggest technical risk — solve this first, not last.
- **Security review debt**: it's tempting to skip the static-analysis pass to move faster; don't — this is the difference between a toy and something a real team will trust with actual internal tools.
- **Scope creep on language/framework support**: resist supporting everything; Python + Node covers the overwhelming majority of agent-generated tools.
- **Self-hosted infra maintenance**: running your own K3s/Postgres/Vault means you own uptime — budget time for this, or plan a managed-service migration path once you have paying users (at which point free/OSS constraint can loosen).

---

*Next steps: run the cold-start spike (Milestone 1) before writing any more code — it determines whether the rest of this architecture is viable as designed.*
