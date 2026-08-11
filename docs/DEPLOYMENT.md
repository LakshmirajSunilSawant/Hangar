# Deploying Hangar

The $0-forever setup, in order. Around 45 minutes if nothing fights back —
most of it is Oracle's signup and the gVisor install.

Verify every free-tier limit as you go. Oracle changed theirs once already in
2026, without announcing it.

---

## What you need before starting

| | Why |
|---|---|
| A machine that stays on, with Linux and Docker | Hangar builds and runs containers. Render, Railway and similar give you *a* container and no daemon, so the control plane would run there and fail at every build |
| A hostname | Apps are served at `<app-name>.<your-domain>` |

**On free hosts:** Railway has no permanent free tier as of 2026 ($5 trial
credit, then $1–5/month), which breaks the $0 rule outright. Render's free tier
is genuine but cannot run Docker. Oracle Always Free can, and doesn't expire.

---

## 1. Claim the VM

Sign up at <https://signup.cloud.oracle.com/>. A payment method is required for
identity verification; Always Free resources are not charged.

Your account begins as a 30-day trial with credits, then drops to Always Free.
**Only build on Always Free-eligible resources** — anything else stops working
when the trial ends.

| Setting | Value |
|---|---|
| Shape | `VM.Standard.A1.Flex` |
| OCPUs / Memory | **2 / 12 GB** — the current Always Free ceiling |
| Image | Canonical Ubuntu 24.04 (**aarch64**) |
| Boot volume | 100–200 GB |
| SSH key | Upload your public key |

> **The 2026 limit cut.** Always Free Ampere dropped from 4 OCPU / 24 GB to
> 2 / 12 on 15 June 2026, and instances above the new limit were terminated
> from 18 August 2026. Ignore any tutorial offering 4 cores and 24 GB.

### "Out of host capacity"

The most common blocker. Ampere is oversubscribed and you may be unable to
create an instance for days. In order of effort:

1. Try each availability domain in your home region.
2. Retry at off-peak hours — capacity is released continuously.
3. Try a smaller shape (1 OCPU / 6 GB) and resize later.
4. Automate retries: <https://github.com/hitrov/oci-arm-host-capacity>

Your home region is fixed at signup, so picking a less popular one during
signup is the cheapest fix.

---

## 2. Open the firewall — both of them

**The single most common Oracle mistake.** There are two independent firewalls
and traffic must pass both.

**a) The VCN security list** (Oracle's web console):
Networking → Virtual Cloud Networks → your VCN → Subnet → Security List →
Add Ingress Rules for source `0.0.0.0/0`, TCP ports **80** and **443**.

**b) iptables on the instance** — Oracle's Ubuntu images ship restrictive rules:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

If a service is unreachable and you're sure it's running, it is almost always
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
between a toy and something a team can trust. Without it, untrusted app code
shares the host kernel with your control plane.

```bash
curl -fsSL https://gvisor.dev/archive.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list

sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install          # registers runsc as a Docker runtime
sudo systemctl reload docker
```

Verify it actually sandboxes — the kernel inside should differ from the host's,
because it *is* a different kernel:

```bash
docker run --rm --runtime=runsc alpine uname -a
uname -a
```

---

## 5. A hostname

Free permanent subdomain from <https://www.duckdns.org/> — sign in, pick a
name, point it at your VM's public IP. Any `*.yourname.duckdns.org` resolves to
the same address, which is exactly what per-app hostnames need.

Keep it current if the IP ever changes:

```bash
# crontab -e
*/5 * * * * curl -s "https://www.duckdns.org/update?domains=YOURNAME&token=YOURTOKEN&ip=" >/dev/null
```

---

## 6. Start Hangar

```bash
git clone https://github.com/LakshmirajSunilSawant/Hangar.git
cd Hangar
cp .env.example .env
```

Fill in `.env`:

```bash
openssl rand -hex 24                                   # POSTGRES_PASSWORD
openssl rand -hex 32                                   # HANGAR_API_TOKEN
docker compose run --rm --no-deps hangar hangar gen-key # HANGAR_SECRET_KEY
```

Set `HANGAR_APP_DOMAIN=yourname.duckdns.org` and `HANGAR_RUNTIME=runsc`, then:

```bash
docker compose up -d
docker compose ps
curl -s localhost:8080/healthz | jq
```

`/healthz` should report `"sandbox_runtime": "runsc"` and `"auth": "enabled"`.
**If it says `docker-default`, you are not sandboxed** — revisit step 4.

That brings up the control plane, Postgres, and Caddy, with apps on an internal
network that has no route out. Caddy obtains certificates on first request.

> **Keep `HANGAR_SECRET_KEY` somewhere durable.** It seals per-app database
> passwords; if it changes, those cannot be read back.

### Your first account

The API token works immediately, but people should have accounts:

```bash
TOKEN=$(grep HANGAR_API_TOKEN .env | cut -d= -f2)
curl -s -X POST localhost:8080/users \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"email":"you@example.com","is_admin":true}'
```

That returns a one-time `invite_token`. Open
`https://yourname.duckdns.org`, choose **I have an invitation**, and set a
password.

---

## 7. Keep it alive

Free account at <https://uptimerobot.com/>, monitoring
`https://yourname.duckdns.org/healthz` every 5 minutes. It tells you when the
box is down *and* keeps activity above Oracle's idle-reclamation threshold.

> Oracle reclaims an instance only when CPU **and** network **and** memory are
> *all* under 20% at the 95th percentile across 7 days. With Postgres, Caddy
> and Docker resident, memory alone will likely keep you clear — the ping is
> cheap insurance rather than the main defence. (The PRD describes this as
> CPU-only; the real policy is stricter and in your favour.)

---

## 8. Backups

Losing the only VM without backups means losing everything.

```bash
sudo apt install -y restic
sudo cp scripts/backup.sh /usr/local/bin/hangar-backup
sudo chmod +x /usr/local/bin/hangar-backup
```

Configure and run it:

```bash
export RESTIC_REPOSITORY=/mnt/backup/hangar   # or Oracle Object Storage
export RESTIC_PASSWORD='...'                  # store this somewhere else
restic init
sudo -E hangar-backup
```

Daily via cron:

```bash
# sudo crontab -e
0 3 * * * RESTIC_REPOSITORY=... RESTIC_PASSWORD=... /usr/local/bin/hangar-backup >> /var/log/hangar-backup.log 2>&1
```

**Test a restore before you need one.** An untested backup is a guess.

---

## Optional: private team access

If you'd rather the tools weren't on the public internet at all:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Teammates install Tailscale and reach the box from anywhere while it stays
invisible to everyone else. Free for **6 users**, unlimited devices.

This is network-level access control, and complements Hangar's own
owner/editor/viewer permissions rather than replacing them.

---

## Reality check

After all this you have a working, always-on, $0 Hangar. Against PRD §8:

| Requirement | State |
|---|---|
| gVisor sandbox | met once step 4 is done |
| Resource caps | met |
| Static scan before first execution | met |
| Egress default-deny | met (`HANGAR_EGRESS=deny`, the compose default) |
| Platform auth in front of apps | met (`HANGAR_APP_AUTH=1`) |
| Per-user permissions | met |
| Secrets encrypted at rest | met for per-app database passwords |

Still open: the Milestone 1 cold-start spike has not been run on this hardware,
and nothing here has faced a real user.

## Upgrading

```bash
cd Hangar && git pull && docker compose up -d --build
```

Deployed apps keep running through a control-plane restart — they are separate
containers, and Hangar reattaches to them by name.
