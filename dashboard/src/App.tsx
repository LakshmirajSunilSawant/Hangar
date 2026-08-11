import { useCallback, useEffect, useState } from "react";
import { ApiError, api, getToken, setToken } from "./api";
import { AppDetail } from "./components/AppDetail";
import { NewApp } from "./components/NewApp";
import { Chip, StatusChip } from "./components/StatusChip";
import { IN_FLIGHT, type App, type Health } from "./types";

export default function Dashboard() {
  const [apps, setApps] = useState<App[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      setApps(await api.list());
      setNeedsToken(false);
      setError("");
    } catch (err) {
      if (err instanceof ApiError && err.isAuthError) setNeedsToken(true);
      else setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    api.health().then(setHealth).catch(() => setHealth(null));
  }, [load]);

  // Keep the list moving while anything is mid-deploy, then stop.
  const anyInFlight = apps.some((a) => IN_FLIGHT.includes(a.status));
  useEffect(() => {
    if (!anyInFlight || selected) return;
    const timer = setInterval(() => void load(), 2000);
    return () => clearInterval(timer);
  }, [anyInFlight, selected, load]);

  if (needsToken) return <TokenGate onSaved={load} />;

  const current = apps.find((a) => a.id === selected) ?? null;

  return (
    <div className="shell">
      <header className="top">
        <h1>Hangar</h1>
        <span className="tagline">Cloud for small software</span>
      </header>

      {health && <HealthBar health={health} />}
      {error && <div className="error">{error}</div>}

      {current ? (
        <AppDetail
          app={current}
          onBack={() => setSelected(null)}
          onChanged={(fresh) =>
            setApps((prev) => prev.map((a) => (a.id === fresh.id ? fresh : a)))
          }
          onDeleted={(id) => {
            setApps((prev) => prev.filter((a) => a.id !== id));
            setSelected(null);
          }}
        />
      ) : creating ? (
        <NewApp
          onCancel={() => setCreating(false)}
          onCreated={(app) => {
            setApps((prev) => [app, ...prev]);
            setCreating(false);
            setSelected(app.id);
          }}
        />
      ) : (
        <>
          <div className="spread" style={{ marginBottom: 12 }}>
            <strong>{apps.length} app{apps.length === 1 ? "" : "s"}</strong>
            <div className="row">
              <button onClick={() => void load()}>Refresh</button>
              <button className="primary" onClick={() => setCreating(true)}>
                Deploy an app
              </button>
            </div>
          </div>
          <AppList apps={apps} loaded={loaded} onSelect={setSelected} />
        </>
      )}
    </div>
  );
}

function AppList({
  apps,
  loaded,
  onSelect,
}: {
  apps: App[];
  loaded: boolean;
  onSelect: (id: string) => void;
}) {
  if (!loaded) return <p className="muted">Loading…</p>;

  if (apps.length === 0) {
    return (
      <div className="card empty">
        <p style={{ marginTop: 0 }}>Nothing deployed yet.</p>
        <p style={{ margin: 0, fontSize: 13 }}>
          Point Hangar at a GitHub repo, a zip, or a directory on the server,
          and it will detect the stack, build it, and hand back a URL.
        </p>
      </div>
    );
  }

  return (
    <div className="apps">
      {apps.map((app) => (
        <button key={app.id} className="app-row" onClick={() => onSelect(app.id)}>
          <div className="grow">
            <div className="name">{app.name}</div>
            <div className="meta">
              {app.runtime ? `${app.runtime} · ${app.framework} · ` : ""}
              {app.source_ref}
            </div>
          </div>
          {app.url && (
            <span className="mono muted" style={{ maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {app.url.replace(/^https?:\/\//, "")}
            </span>
          )}
          <StatusChip status={app.status} />
        </button>
      ))}
    </div>
  );
}

function HealthBar({ health }: { health: Health }) {
  // The sandbox line is the one that matters: "docker-default" means untrusted
  // code shares the host kernel, which the PRD treats as non-negotiable.
  const sandboxed = health.sandbox_runtime !== "docker-default";
  return (
    <div className="health">
      <Chip tone={health.backend_available ? "ok" : "bad"}>
        backend: {health.backend}
        {health.backend_available ? "" : " (unavailable)"}
      </Chip>
      <Chip tone={sandboxed ? "ok" : "warn"}>
        sandbox: {sandboxed ? health.sandbox_runtime : "docker (shares host kernel)"}
      </Chip>
      <Chip tone={health.auth === "enabled" ? "ok" : "warn"}>auth: {health.auth}</Chip>
      {health.router !== "none" && (
        <Chip tone={health.router_available ? "ok" : "bad"}>
          router: {health.router}
        </Chip>
      )}
    </div>
  );
}

function TokenGate({ onSaved }: { onSaved: () => void }) {
  const [value, setValue] = useState(getToken());

  return (
    <div className="shell">
      <form
        className="card gate"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(value.trim());
          onSaved();
        }}
      >
        <h1 style={{ fontSize: 20, marginTop: 0 }}>Hangar</h1>
        <p className="muted" style={{ fontSize: 14 }}>
          This control plane requires an API token.
        </p>
        <div className="field">
          <label htmlFor="token">API token</label>
          <input
            id="token"
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            autoFocus
          />
          <div className="hint">The value of HANGAR_API_TOKEN on the server.</div>
        </div>
        <button className="primary" type="submit" disabled={!value.trim()}>
          Continue
        </button>
      </form>
    </div>
  );
}
