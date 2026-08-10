# Deploying Hangar on an Oracle Always Free VM

The $0-forever setup, in order. Roughly 90 minutes if nothing fights back.

Verify every free-tier limit as you go — Oracle changed theirs once already in
2026, without announcing it.

---

## 0. Before you start

**Why Oracle and not Render/Railway.** Hangar builds and runs containers.
Render and Railway hand you a container and no Docker daemon, so the control
plane would run there but would fail at every build. Railway also has no
permanent free tier as of 2026 ($5 trial credit, then $1–5/month), which breaks
the project's $0 rule outright.

**What you get free, permanently:** 2 OCPU / 12 GB RAM / 200 GB block storage
on Ampere ARM, 10 TB/month egress, 20 GB object storage.

---

## 1. Claim the VM

Sign up at <https://signup.cloud.oracle.com/>. A payment method is required for
identity verification; Always Free resources are not charged.

Your account begins as a 30-day trial with credits, then drops to Always Free.
**Only build on Always Free-eligible resources** — anything else stops working
when the trial ends.

Create the instance:

| Setting | Value |
|---|---|
| Shape | `VM.Standard.A1.Flex` |
| OCPUs / Memory | **2 / 12 GB** — the current Always Free ceiling |
| Image | Canonical Ubuntu 24.04 (**aarch64**) |
| Boot volume | 100–200 GB |
| SSH key | Upload your public key |

> **The 2026 limit cut.** Always Free Ampere dropped from 4 OCPU / 24 GB to
> 2 / 12 on 15 June 2026, and instances above the new limit were terminated from
> 18 August 2026. Ignore any tutorial offering 4 cores and 24 GB.

### "Out of host capacity"

The most common blocker. Ampere is oversubscribed and you may be unable to
create an instance for days. Options, in order of effort:

1. Try each availability domain in your home region.
2. Retry at off-peak hours — capacity is released continuously.
3. Try a smaller shape (1 OCPU / 6 GB) and resize later.
4. Automate retries: <https://github.com/hitrov/oci-arm-host-capacity>

Your home region is fixed at signup, so choosing a less popular one during
signup is the cheapest fix.

---

## 2. Open the firewall — both of them

**The single most common Oracle mistake.** There are two independent firewalls
and traffic must pass both.

**a) The VCN security list** (Oracle's web console):
Networking → Virtual Cloud Networks → your VCN → Subnet → Security List →
Add Ingress Rules for source `0.0.0.0/0`, TCP ports **80** and **443**.

**b) iptables on the instance** — Oracle's Ubuntu images ship with restrictive
rules:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

If a service is unreachable and you're sure it's running, it's almost always
one of these two.

---

## 3. Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker run --rm hello-world
```

---

## 4. gVisor — the actual sandbox

**Do not skip this.** Everything else is convenience; this is the difference
between a toy and something a team can trust with real tools. Without it,
untrusted app code shares the host kernel with your control plane.

```bash
curl -fsSL https://gvisor.dev/archive.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list

sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install          # registers runsc as a Docker runtime
sudo systemctl reload docker
```

Verify it actually sandboxes — the kernel version inside should differ from the
host's, because it *is* a different kernel:

```bash
docker run --rm --runtime=runsc alpine uname -a
uname -a
```

Then point Hangar at it:

```bash
export HANGAR_RUNTIME=runsc
```

Hangar has a test asserting this setting reaches Docker, so if it's wrong,
deploys fail loudly rather than silently running unsandboxed.

---

## 5. A hostname

Free permanent subdomain from <https://www.duckdns.org/> — sign in, pick a
name, point it at your VM's public IP.

Keep it current if the IP ever changes:

```bash
# crontab -e
*/5 * * * * curl -s "https://www.duckdns.org/update?domains=YOURNAME&token=YOURTOKEN&ip=" >/dev/null
```

A registered `.com` is nicer but costs money every year, which breaks the $0
rule.

---

## 6. Caddy — routing and HTTPS

Caddy gets Let's Encrypt certificates automatically and renews them. No cron,
no certbot.

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

`/etc/caddy/Caddyfile`:

```caddyfile
yourname.duckdns.org {
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo systemctl reload caddy
```

HTTPS should work within seconds. If it doesn't, revisit step 2 — Let's Encrypt
has to reach port 80 to issue the certificate.

> **Not built yet:** per-app routing. Hangar still returns
> `http://localhost:<random-port>`, so apps aren't shareable until the deploy
> engine writes Caddy config per app. That's the next piece of work.

---

## 7. Postgres

```bash
docker run -d --name hangar-db --restart unless-stopped \
  -e POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  -e POSTGRES_DB=hangar \
  -v hangar-pgdata:/var/lib/postgresql/data \
  -p 127.0.0.1:5432:5432 \
  postgres:16-alpine
```

Note `127.0.0.1:` in the port mapping — without it Docker publishes to every
interface and **bypasses your iptables rules**, exposing the database to the
internet.

---

## 8. Hangar

```bash
git clone https://github.com/LakshmirajSunilSawant/Hangar.git
cd Hangar
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv && uv pip install -e ".[postgres]"
```

Configuration:

```bash
export DATABASE_URL="postgresql://postgres:PASSWORD@localhost:5432/hangar"
export HANGAR_API_TOKEN="$(openssl rand -hex 32)"   # save this
export HANGAR_RUNTIME=runsc
export HANGAR_PUBLIC_BASE_URL="https://yourname.duckdns.org"
export HANGAR_APP_MEMORY_MB=384                     # 12 GB shared between apps
```

Check what resolved, then run it:

```bash
uv run hangar config
uv run hangar serve --host 127.0.0.1 --port 8080
```

Bind to `127.0.0.1` and let Caddy handle the public side — Caddy terminates TLS,
Hangar never needs to face the internet directly. (Hangar refuses to bind
publicly without `HANGAR_API_TOKEN` set, but there's no reason to bind publicly
at all behind a proxy.)

### systemd

`/etc/systemd/system/hangar.service`:

```ini
[Unit]
Description=Hangar control plane
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Hangar
EnvironmentFile=/home/ubuntu/Hangar/.env
ExecStart=/home/ubuntu/.local/bin/uv run hangar serve --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5

# Untrusted app containers run under gVisor; the control plane itself
# still shouldn't be able to escalate or share /tmp.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Put the exports in `/home/ubuntu/Hangar/.env` (`chmod 600` — it holds the
database password and API token), then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hangar
curl -s localhost:8080/healthz | jq
```

`/healthz` should report `"sandbox_runtime": "runsc"` and
`"auth": "enabled"`. If it says `docker-default`, gVisor isn't wired and you are
not sandboxed.

---

## 9. Keep it alive

Free account at <https://uptimerobot.com/>, monitor
`https://yourname.duckdns.org/healthz` every 5 minutes.

This does two jobs: tells you when the box is down, and keeps activity above
Oracle's idle-reclamation threshold.

> Oracle reclaims an instance only when CPU **and** network **and** memory are
> *all* under 20% at the 95th percentile across 7 days. With Postgres, Caddy,
> and Docker resident, memory alone will likely keep you clear — the ping is
> cheap insurance, not the main defence. (The PRD describes this as CPU-only;
> the real policy is stricter and in your favour.)

---

## 10. Backups

Losing the only VM without backups means losing everything.

```bash
sudo apt install -y restic
export RESTIC_REPOSITORY="/mnt/backup/hangar"     # or Oracle Object Storage
export RESTIC_PASSWORD="..."                       # store this somewhere else
restic init

docker exec hangar-db pg_dump -U postgres hangar > /tmp/hangar.sql
restic backup /tmp/hangar.sql /home/ubuntu/Hangar/.env
restic forget --keep-daily 7 --keep-weekly 4 --prune
```

Put it on a daily cron. **Test a restore before you need one** — an untested
backup is a guess.

---

## Optional: private team access via Tailscale

If you'd rather the tools weren't on the public internet at all:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Teammates install Tailscale and reach the box from anywhere, while it stays
invisible to everyone else. Free for **6 users**, unlimited devices — past that
it's paid.

This gives you network-level access control today, doing part of what PRD §8's
auth layer is meant to do. It is not a substitute for per-app permissions.

---

## Reality check

After all this you have a working, always-on, $0 Hangar — but these PRD §8
requirements are still unmet in the code:

| Requirement | State |
|---|---|
| gVisor sandbox | **met** once step 4 is done |
| Resource caps | met |
| Static scan before first execution | **not built** |
| Egress default-deny | **not built** |
| Per-user auth and permissions | **not built** (shared token only) |
| Secrets injected at runtime | **not built** |

Until the scan and egress rules land, deploy code you wrote or have read.
