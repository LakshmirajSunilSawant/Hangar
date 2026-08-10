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
| Ingestion: local folder path | working |
| Pluggable execution backend | working |
| Env-driven config, Postgres support | working |
| API token auth on the control plane | working |
| Ingestion: zip upload / GitHub repo | not started |
| Caddy routing / stable per-app hostnames | not started |
| Static security scan (Semgrep / Bandit / osv-scanner) | not started |
| Owner dashboard (React + Vite) | not started |
| Auth + permissions (Ory Kratos) | not started |
| Per-app database provisioning | not started |
| Egress deny-by-default | not started |

Measured locally (x86, WSL2, warm base image layers):

| Sample app | Build | Cold start → HTTP 200 |
|---|---|---|
| `examples/fastapi-hello` | 20.0s | 1.70s |
| `examples/express-hello` | 17.1s | 0.78s |

Both are inside the PRD's <3s cold-start target, but this is *not* the Milestone 1 go/no-go answer — that needs Ampere ARM hardware running gVisor, not a Windows dev box running plain Docker.

### A note on the sandbox

The PRD specifies **gVisor/Kata** for isolating untrusted code — that is the real security boundary and it is non-negotiable for anything user-facing. This local build uses **plain Docker containers** as a stand-in so the pipeline can be developed on a Windows dev machine. Docker's default runtime shares the host kernel and is *not* an adequate sandbox for untrusted code. Swapping in gVisor is a prerequisite for deploying anything you didn't write yourself.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Docker (daemon running)

To run this on a server, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — the
$0-forever setup on an Oracle Always Free VM, including the gVisor sandbox.

## Setup

```bash
uv venv
uv pip install -e ".[dev]"
```

## Usage

Start the control plane:

```bash
uv run hangar serve            # http://127.0.0.1:8080, docs at /docs
```

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
| `POST` | `/apps` | Register a source directory and start a deploy |
| `GET` | `/apps` | List apps |
| `GET` | `/apps/{id}` | One app: status, URL, detected runtime |
| `GET` | `/apps/{id}/logs` | Build log and container log |
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
| `PORT` | `8080` | Port to serve on (what most hosts inject) |

For Postgres, install the driver: `uv pip install -e ".[postgres]"`.

### Authentication

Routes under `/apps` require `Authorization: Bearer $HANGAR_API_TOKEN` when that
variable is set. `/healthz` stays open so uptime pingers and platform probes work
without credentials.

With no token set the API is unauthenticated, which keeps local development
setup-free — and `hangar serve` **refuses to bind to a non-loopback interface**
in that state, so an anonymous control plane can't be exposed by accident.

This guards the control plane's own API. It is *not* the PRD's auth layer:
per-user identity and owner/editor/viewer permissions via Ory Kratos are still
Milestone 3.

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
uv run pytest                  # everything (builds real images, ~25s)
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
  store.py       persistence (App, Deployment) on SQLite or Postgres
  config.py      environment-driven settings
  auth.py        shared-token API auth
  api.py         FastAPI control plane
  cli.py         `hangar serve` / `hangar config`
tests/           test suite; `slow` marker = needs Docker
examples/        sample apps used to exercise the pipeline
```

## Security posture today

Per PRD §8, and honestly labelled:

| Requirement | State |
|---|---|
| Hard resource caps (CPU, memory, PIDs) | enforced |
| Non-root, `cap_drop: ALL`, no-new-privileges, read-only rootfs | enforced |
| Untrusted code never executed during detection | enforced (static analysis only) |
| gVisor/Kata sandbox | **not yet** — plain Docker, shares the host kernel |
| Static scan before first execution | **not yet** |
| Egress default-deny | **not yet** |
| Platform-level auth in front of apps | **not yet** |

Until the first three of those land, only deploy code you wrote or trust.
