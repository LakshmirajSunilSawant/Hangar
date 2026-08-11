# Hangar

**Cloud for small software.** Point Hangar at a generated app — a folder or a zip — and get back a live URL you can share.

Built for the gap that agents (Claude Code, Cursor, v0) opened up: generating a small internal tool is now trivial, but getting it from "runs on my laptop" to "a live tool my team can use, with the right people having access" still isn't. See [`cloud-for-small-software-PRD.md`](cloud-for-small-software-PRD.md) for the full product rationale.

Every component is free-to-use or open source. No paid SaaS dependencies.

---

## Status

The **thin vertical slice works**: source directory → runtime detection → image build → sandboxed container → live URL, driven through the API. No auth, permissions, or per-app databases yet.

| Piece | State |
|---|---|
| Runtime detection (Python / Node) | working |
| Image builder + Docker deploy engine | working |
| Control plane API (FastAPI) | working |
| Ingestion: local path, zip upload, GitHub repo | working |
| Pluggable execution backend | working |
| Env-driven config, Postgres support | working |
| API token auth on the control plane | working |
| Caddy routing / stable per-app hostnames | working |
| Static security scan before execution | working |
| Egress default-deny | working |
| Owner dashboard (React + Vite) | working |
| Users, owner/editor/viewer permissions | working |
| Platform-level auth in front of apps | working |
| Per-app databases (SQLite or Postgres) | working |
| CI (GitHub Actions) | working |
| One-command deploy (Docker Compose) | working |
| Backups (restic) | working |
| `hangar deploy` CLI | working |

Measured on x86 (WSL2, warm base image layers):

| Sample app | Runtime | Build | Cold start → HTTP 200 |
|---|---|---|---|
| `examples/fastapi-hello` | plain Docker | 20.0s | 1.70s |
| `examples/express-hello` | plain Docker | 17.1s | 0.78s |
| `examples/fastapi-hello` | **gVisor (`runsc`)** | 19.1s | **2.34s** |

The gVisor number is the interesting one: a real sandbox, kernel `4.19.0-gvisor`,
still inside the PRD's <3s target. Sandboxing cost about 0.6s.

**This is still not the Milestone 1 answer.** It is x86 rather than Ampere ARM,
WSL2 rather than bare metal, and "cold start" here means stopping and restarting
an existing container — Hangar has no scale-to-zero, so the idle-app-reopened
case the PRD actually targets does not exist yet. Evidence, not a verdict.

### A note on the sandbox

The PRD specifies **gVisor/Kata** for isolating untrusted code — that is the real security boundary and it is non-negotiable for anything user-facing. This local build uses **plain Docker containers** as a stand-in so the pipeline can be developed on a Windows dev machine. Docker's default runtime shares the host kernel and is *not* an adequate sandbox for untrusted code. Swapping in gVisor is a prerequisite for deploying anything you didn't write yourself.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker (daemon running)

To run this on a server, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — the
$0-forever setup on an Oracle Always Free VM, including the gVisor sandbox.

## Setup

**On a server**, one command brings up the control plane, Postgres and Caddy:

```bash
cp .env.example .env    # fill in the generated secrets
docker compose up -d
```

**For development**, run it directly:

```bash
uv venv
uv pip install -e ".[dev]"
```

## Usage

Build the dashboard once, then start the control plane:

```bash
cd dashboard && npm install && npm run build && cd ..
uv run hangar serve            # http://127.0.0.1:8080
```

That serves the owner dashboard at `/`, the API under `/apps`, and OpenAPI docs
at `/docs`. The dashboard is optional — the API works without it.

Deploy an app by pointing at a source directory:

```bash
curl -X POST http://127.0.0.1:8080/apps \
  -H 'content-type: application/json' \
  -d '{"name":"fastapi-hello","source_path":"/absolute/path/to/examples/fastapi-hello"}'
```

That returns `202` immediately with a `queued` app. Poll it until it's running:

```bash
curl http://127.0.0.1:8080/apps/<id>        # status, url, runtime, framework
curl http://127.0.0.1:8080/apps/<id>/logs   # build log + container log
```

| Method | Route | Does |
|---|---|---|
| `POST` | `/apps` | Register a source directory or GitHub repo, and deploy |
| `POST` | `/apps/upload` | Upload a zip of the app's source and deploy |
| `GET` | `/apps` | List apps |
| `GET` | `/apps/{id}` | One app: status, URL, detected runtime |
| `GET` | `/apps/{id}/logs` | Build log and container log |
| `GET` | `/apps/{id}/scan` | Security findings from the pre-execution scan |
| `POST` | `/apps/{id}/redeploy` | Rebuild and replace the container |
| `POST` | `/apps/{id}/stop` | Stop the container |
| `POST` | `/apps/{id}/restart` | Restart and re-read the published port |
| `DELETE` | `/apps/{id}` | Remove the container and forget the app |

## Configuration

Everything deployment-varying comes from the environment, so the same image runs
on a laptop and on a server. `hangar config` prints what's resolved (credentials
redacted).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` / `HANGAR_DATABASE_URL` | local SQLite | Control-plane database. `postgres://` URLs are normalised automatically |
| `HANGAR_API_TOKEN` | unset | Shared bearer token for `/apps`. Unset = no auth, loopback only |
| `HANGAR_BACKEND` | `docker` | Which execution backend runs apps |
| `HANGAR_RUNTIME` | unset | Container runtime. Set to `runsc` for gVisor |
| `HANGAR_PUBLIC_BASE_URL` | `http://localhost` | Base for generated app URLs |
| `HANGAR_APP_MEMORY_MB` | `512` | Per-app memory cap |
| `HANGAR_APP_CPUS` | `0.5` | Per-app CPU cap |
| `HANGAR_APP_PIDS` | `256` | Per-app process cap |
| `HANGAR_ROUTER` | `none` | `caddy` gives apps stable hostnames |
| `HANGAR_APP_DOMAIN` | unset | Apps are served at `<name>.<domain>` |
| `HANGAR_APP_SCHEME` | `https` | Scheme for generated app URLs |
| `HANGAR_CADDY_ADMIN_URL` | `http://localhost:2019` | Caddy's admin API |
| `HANGAR_CADDY_SERVER` | `srv0` | Which Caddy server holds the routes |
| `HANGAR_UPSTREAM_HOST` | `127.0.0.1` | Where Caddy dials app containers |
| `HANGAR_SCAN_POLICY` | `flag` | `flag`, `block`, or `off` |
| `HANGAR_SCAN_BLOCK_SEVERITY` | `high` | Threshold when policy is `block` |
| `HANGAR_EGRESS` | `allow` | `deny` removes apps' outbound network |
| `HANGAR_APP_NETWORK` | `hangar-apps` | Internal network used when denying egress |
| `HANGAR_SOURCE_ROOT` | `.hangar/sources` | Where uploaded and fetched sources are extracted |
| `HANGAR_GITHUB_TOKEN` | unset | Raises the GitHub rate limit; required for private repos |
| `HANGAR_APP_DB` | `none` | Default per-app database: `none`, `sqlite`, `postgres` |
| `HANGAR_APP_DB_ADMIN_URL` | unset | Postgres Hangar may create databases/roles on |
| `HANGAR_SECRET_KEY` | unset | Seals stored secrets. Generate with `hangar gen-key` |
| `HANGAR_IDENTITY` | `local` | Identity provider |
| `HANGAR_SESSION_HOURS` | `336` | Session lifetime |
| `HANGAR_APP_AUTH` | `0` | `1` requires sign-in before reaching any app |
| `HANGAR_COOKIE_DOMAIN` | unset | Scopes the session across subdomains; required with app auth |
| `HANGAR_CONTROL_PLANE_ADDRESS` | `127.0.0.1:8080` | Where the proxy reaches Hangar for forward-auth |
| `PORT` | `8080` | Port to serve on (what most hosts inject) |

For Postgres, install the driver: `uv pip install -e ".[postgres]"`.

### Authentication

Routes under `/apps` require `Authorization: Bearer $HANGAR_API_TOKEN` when that
variable is set. `/healthz` stays open so uptime pingers and platform probes work
without credentials.

With no token set the API is unauthenticated, which keeps local development
setup-free — and `hangar serve` **refuses to bind to a non-loopback interface**
in that state, so an anonymous control plane can't be exposed by accident.

The token is for scripts and CI. People sign in instead, and are scoped to the
apps they've been granted:

| Role | Can |
|---|---|
| **owner** | view, logs, deploy, share, delete |
| **editor** | view, logs, deploy |
| **viewer** | view only — *not* logs, which can contain anything the app printed |

Accounts are invite-based; Hangar sends no email (a mail service costs money),
so `POST /users` returns a one-time token you pass to the person, the way you'd
share a document link.

```bash
curl -X POST /users -d '{"email":"teammate@example.com"}'   # returns invite_token
curl -X PUT /apps/<id>/access -d '{"email":"teammate@example.com","role":"viewer"}'
```

A caller with no access to an app gets **404, not 403** — whether an app exists
is itself information.

### Auth in front of the apps themselves

PRD §8 wants recipients authenticated *before* they reach an app's own routes.
With `HANGAR_APP_AUTH=1`, Caddy asks Hangar about every request via forward-auth
and only then proxies it, adding `X-Hangar-User`, `X-Hangar-User-Id` and
`X-Hangar-Role`.

`examples/whoami` demonstrates it: the app contains no login code, no sessions
and no user table, yet knows exactly who is visiting. Only trust those headers
when the app is reachable *solely* through the proxy — pair this with
`HANGAR_EGRESS=deny`, which takes apps off the host network entirely.

### On Ory Kratos

The PRD names Kratos. Kratos is a separate Go service with its own database,
which is a lot to ask of a 12 GB box already running Postgres, Caddy and the
sandboxes — so identity sits behind an `IdentityProvider` interface with a
built-in provider that needs no extra services. A Kratos provider implements
three methods and changes one env var.

### Routing

By default apps come back as `http://localhost:<random-port>` — fine on one
machine, useless for sharing. With `HANGAR_ROUTER=caddy` and an
`HANGAR_APP_DOMAIN`, each app gets a stable hostname instead:

```
sales-tool.apps.example.com   →  container on :64800
standup-bot.apps.example.com  →  container on :51203
```

Hangar drives Caddy's admin API directly, so there's no Caddyfile to write and
no reload to sequence. Routes are tagged `hangar-<app_id>`, which makes updates
idempotent and leaves any other sites Caddy serves alone. The hostname survives
restarts even when the container's port changes — that's the point of it.

Routes are inserted at the front of Caddy's route list. Caddy matches routes in
order, so a route appended behind a catch-all is never reached.

### Swapping the sandbox

`runtime.py` talks to Docker, but the control plane only knows the
`ExecutionBackend` interface in `hangar/backends/base.py`. That's the seam for
the PRD's target architecture, where execution lives on a different box from the
control plane — a remote-runner backend implements the same methods without
touching this code.

On a host with gVisor installed, `HANGAR_RUNTIME=runsc` routes app containers
through it.

## Development

```bash
cd dashboard && npm run dev    # dashboard on :5173, proxying the API to :8080
uv run pytest                  # everything (builds real images, ~75s)
uv run pytest -m "not slow"    # fast tests only, no Docker needed
```

The Postgres tests need a throwaway database and are skipped without one:

```bash
docker run -d --name hangar-test-pg -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=hangar -p 55432:5432 postgres:16-alpine
HANGAR_TEST_POSTGRES_URL=postgresql://postgres:test@localhost:55432/hangar \
  uv run pytest tests/test_store_postgres.py
```

## Layout

```
hangar/
  detect.py      static runtime/framework detection — never executes app code
  builder.py     Dockerfile generation and image builds
  runtime.py     Docker container lifecycle, resource caps, logs
  backends/
    base.py      the ExecutionBackend interface — no Docker import
    docker_backend.py  local Docker implementation
  deploy.py      orchestration: detect -> build -> run, with status transitions
  routing.py     per-app hostnames via Caddy's admin API
  scan.py        static security analysis — never executes what it scans
  ingest.py      zip and GitHub sources, with hostile-archive handling
  database.py    per-app SQLite volumes and Postgres databases
  secrets.py     libsodium sealing for stored credentials
  identity.py    invitations, passwords, sessions; provider interface
  permissions.py the owner/editor/viewer capability table
  routes_auth.py sign-in, invitations, sharing
  store.py       persistence (App, Deployment) on SQLite or Postgres
  config.py      environment-driven settings
  auth.py        shared-token API auth
  api.py         FastAPI control plane
  cli.py         `hangar serve` / `deploy` / `config` / `gen-key`
  client.py      HTTP client for deploying to a remote Hangar
dashboard/       React + Vite owner UI, served by the control plane
scripts/         backup.sh and restore.sh (restic)
tests/           test suite; `slow` marker = needs Docker
examples/        sample apps used to exercise the pipeline
```

## Security posture today

Per PRD §8, and honestly labelled:

| Requirement | State |
|---|---|
| Hard resource caps (CPU, memory, PIDs) | enforced |
| Non-root, `cap_drop: ALL`, no-new-privileges, read-only rootfs | enforced |
| Untrusted code never executed during detection or scanning | enforced |
| Static scan before first execution | enforced (`HANGAR_SCAN_POLICY`) |
| Egress default-deny | available (`HANGAR_EGRESS=deny`), off by default |
| gVisor/Kata sandbox | wired (`HANGAR_RUNTIME=runsc`), needs a Linux host |
| Platform-level auth in front of apps | **not yet** — shared token on the API only |
| Secrets injected at runtime, never in code | **not yet** |

Until gVisor is actually in use, the kernel is shared with the host — only
deploy code you wrote or have read.

### Security scanning

Every deploy is scanned **before** the build, because building installs the
app's declared dependencies and that runs their setup code. By the time an
image exists, untrusted code has already had a turn.

The built-in scanner always runs and needs nothing installed — Python via
`ast`, JavaScript via patterns — covering what PRD §8 names: `eval`/`exec`,
shell execution, filesystem escapes, raw sockets, unsafe deserialisation. A
security gate assembled only from optional tools does nothing at all on a
machine where none are present, while still reporting success.

Bandit, Semgrep, and osv-scanner are used when on `PATH` and recorded as
skipped, with a reason, when not.

```bash
curl -H "Authorization: Bearer $HANGAR_API_TOKEN" \
  http://127.0.0.1:8080/apps/<id>/scan
```

`HANGAR_SCAN_POLICY=flag` (default, per PRD v1) records findings and continues;
`block` refuses any deploy with a finding at or above
`HANGAR_SCAN_BLOCK_SEVERITY`; `off` skips it.

### Per-app databases

Apps run on a read-only root filesystem, so without this they can persist
nothing at all — `/tmp` is the only writable path and it is ephemeral.

```bash
curl -X POST /apps -d '{"name":"notes","repo_url":"owner/repo","database":"sqlite"}'
```

`DATABASE_URL` is injected into the container. Two modes:

- **sqlite** — a Docker volume mounted at `/data`. No server, no credentials,
  no network; keeps working when egress is denied. The right default for a
  small internal tool.
- **postgres** — a dedicated database *and* role, not a shared schema, so one
  app cannot read another's tables even if it goes looking. Needs
  `HANGAR_APP_DB_ADMIN_URL` and `HANGAR_SECRET_KEY`.

Generated Postgres passwords are sealed with libsodium before they touch the
control-plane database, per PRD §8. `HANGAR_SECRET_KEY` is never auto-generated
— a key that changed on restart would silently orphan every stored secret.

Deleting an app destroys its database unless you pass `?keep_data=true`.

### Uploaded and fetched sources

Archives are the most hostile input the platform takes, and extraction happens
on the **control-plane host, before any sandbox exists** — so a path-traversal
bug there writes to the host as the Hangar process.

Extraction therefore does not use `extractall`. Every entry's resolved
destination must stay inside the target directory; symlinks, hardlinks and
device nodes are dropped (a symlink is a traversal primitive that survives the
path check); and total expanded size and entry count are capped so a small
archive can't become a disk-filling one.

GitHub is read through the REST API's tarball endpoint rather than by shelling
out to `git`, so no git binary is needed on the host. Set `HANGAR_GITHUB_TOKEN`
for private repos or to raise the rate limit.

### Egress deny

`HANGAR_EGRESS=deny` puts apps on an internal Docker network with no route off
the host — verified in the test suite by making a real outbound request from
inside a sandbox and requiring it to fail, for both HTTP and DNS.

The trade-off is unavoidable rather than a design choice: a container on an
internal network **cannot publish a port to the host**, so apps are reachable
only through a proxy attached to the same network. Hangar refuses to start with
`HANGAR_EGRESS=deny` and `HANGAR_ROUTER=none` rather than leave apps silently
unreachable. Attach Caddy to `$HANGAR_APP_NETWORK` when using this mode.
