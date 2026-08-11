# The Hangar control plane, with the dashboard built in.
#
# Note what this image is *not*: it does not sandbox anything. It talks to a
# Docker daemon to create the sandboxes, which means it needs the daemon's
# socket, which is equivalent to root on the host. Treat this container as a
# privileged component and keep it off the app network — see docker-compose.yml.

# --- dashboard -------------------------------------------------------------
FROM node:22-slim AS dashboard

WORKDIR /dashboard
# Dependencies first, so editing a component doesn't reinstall node_modules.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ ./
RUN npm run build


# --- control plane ---------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HANGAR_DASHBOARD_DIR=/app/dashboard/dist

WORKDIR /app

# git is not required to fetch repos — Hangar uses the GitHub REST API — so
# the image stays without it.
COPY pyproject.toml README.md ./
COPY hangar/ ./hangar/
RUN pip install --no-cache-dir ".[postgres]"

COPY --from=dashboard /dashboard/dist ./dashboard/dist

# Runs as root because it needs the Docker socket, which is root-equivalent
# regardless. Dropping to a non-root user here would be cosmetic: whoever can
# reach the socket can start a privileged container.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["hangar", "serve", "--host", "0.0.0.0", "--port", "8080"]
