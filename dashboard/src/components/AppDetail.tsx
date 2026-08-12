import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { IN_FLIGHT, type App, type Logs, type Scan } from "../types";
import { Sharing } from "./Sharing";
import { ScanChip, SeverityChip, StatusChip } from "./StatusChip";
import { Usage } from "./Usage";

type Tab = "logs" | "usage" | "scan" | "people";

export function AppDetail({
  app,
  idleTimeout,
  onBack,
  onChanged,
  onDeleted,
}: {
  app: App;
  /** Seconds before an unused app sleeps; 0 when scale-to-zero is off. */
  idleTimeout: number;
  onBack: () => void;
  onChanged: (app: App) => void;
  onDeleted: (id: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("logs");
  const [logs, setLogs] = useState<Logs | null>(null);
  const [scan, setScan] = useState<Scan | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [fresh, freshLogs] = await Promise.all([
        api.get(app.id),
        api.logs(app.id).catch(() => null),
      ]);
      onChanged(fresh);
      if (freshLogs) setLogs(freshLogs);
      // A scan only exists once a deployment has run.
      setScan(await api.scan(app.id).catch(() => null));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [app.id, onChanged]);

  useEffect(() => {
    void refresh();
  }, [app.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Building can take half a minute; poll until it settles.
  useEffect(() => {
    if (!IN_FLIGHT.includes(app.status)) return;
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [app.status, refresh]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setError("");
    try {
      await fn();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!confirm(`Delete ${app.name}? This stops the app and removes its container.`))
      return;
    setBusy("delete");
    try {
      await api.remove(app.id);
      onDeleted(app.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy("");
    }
  }

  return (
    <div>
      <button className="link" onClick={onBack} style={{ marginBottom: 16 }}>
        ← All apps
      </button>

      <div className="detail-head">
        <div style={{ flex: 1, minWidth: 200 }}>
          <h2>{app.name}</h2>
          <div className="row">
            <StatusChip status={app.status} />
            {scan && <ScanChip status={scan.status} />}
          </div>
        </div>
        <div className="row">
          <button disabled={!!busy} onClick={() => act("redeploy", () => api.redeploy(app.id))}>
            {busy === "redeploy" ? "Redeploying…" : "Redeploy"}
          </button>
          {app.status === "running" && idleTimeout > 0 && (
            // Stop keeps the app down until someone starts it; sleep keeps the
            // URL live and lets the next visit bring it back.
            <button disabled={!!busy} onClick={() => act("sleep", () => api.sleep(app.id))}>
              {busy === "sleep" ? "Sleeping…" : "Sleep"}
            </button>
          )}
          {app.status === "running" ? (
            <button disabled={!!busy} onClick={() => act("stop", () => api.stop(app.id))}>
              Stop
            </button>
          ) : app.status === "sleeping" ? (
            // A visit wakes it anyway; this is for warming it before a demo.
            <button disabled={!!busy} onClick={() => act("wake", () => api.wake(app.id))}>
              {busy === "wake" ? "Waking…" : "Wake"}
            </button>
          ) : (
            <button disabled={!!busy} onClick={() => act("restart", () => api.restart(app.id))}>
              Start
            </button>
          )}
          <button className="danger" disabled={!!busy} onClick={remove}>
            Delete
          </button>
        </div>
      </div>

      {app.error && <div className="error">{app.error}</div>}
      {error && <div className="error">{error}</div>}

      <div className="facts card" style={{ marginBottom: 16 }}>
        <Fact k="URL">
          {app.url ? (
            <a href={app.url} target="_blank" rel="noreferrer">{app.url}</a>
          ) : (
            <span className="muted">not running</span>
          )}
          {app.status === "sleeping" && (
            <div className="muted" style={{ fontSize: 12 }}>
              Asleep to save memory. The link still works — the first visit
              takes a moment longer while it starts.
            </div>
          )}
        </Fact>
        <Fact k="Stack">
          {app.runtime ? `${app.runtime} · ${app.framework}` : <span className="muted">—</span>}
        </Fact>
        <Fact k="Source">
          <span className="mono">{app.source_ref}</span>
          <div className="muted" style={{ fontSize: 12 }}>
            {app.source_type}
            {app.source_revision ? ` · ${app.source_revision}` : ""}
          </div>
        </Fact>
        <Fact k="Updated">{new Date(app.updated_at).toLocaleString()}</Fact>
      </div>

      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "logs"} onClick={() => setTab("logs")}>
          Logs
        </button>
        <button role="tab" aria-selected={tab === "usage"} onClick={() => setTab("usage")}>
          Usage
        </button>
        <button role="tab" aria-selected={tab === "scan"} onClick={() => setTab("scan")}>
          Security{scan && scan.findings.length > 0 ? ` (${scan.findings.length})` : ""}
        </button>
        <button role="tab" aria-selected={tab === "people"} onClick={() => setTab("people")}>
          People
        </button>
      </div>

      {tab === "logs" && <LogsPane logs={logs} />}
      {tab === "usage" && <Usage appId={app.id} live={app.status === "running"} />}
      {tab === "scan" && <ScanPane scan={scan} />}
      {tab === "people" && <Sharing appId={app.id} />}
    </div>
  );
}

function Fact({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="fact">
      <div className="k">{k}</div>
      <div className="v">{children}</div>
    </div>
  );
}

function LogsPane({ logs }: { logs: Logs | null }) {
  if (!logs) return <p className="muted">Loading logs…</p>;
  return (
    <>
      <h3 style={{ fontSize: 13, margin: "0 0 6px" }}>Build</h3>
      <pre className="log">{logs.build_log || "No build output yet."}</pre>
      <h3 style={{ fontSize: 13, margin: "0 0 6px" }}>Application</h3>
      <pre className="log">{logs.runtime_log || "No output from the container yet."}</pre>
    </>
  );
}

function ScanPane({ scan }: { scan: Scan | null }) {
  if (!scan) return <p className="muted">No scan yet — deploy the app first.</p>;

  return (
    <div>
      <div className="row" style={{ marginBottom: 12 }}>
        <ScanChip status={scan.status} />
        <span className="muted" style={{ fontSize: 13 }}>
          policy: {scan.policy} · {scan.counts.high ?? 0} high, {scan.counts.medium ?? 0} medium,{" "}
          {scan.counts.low ?? 0} low
        </span>
      </div>

      <p className="hint" style={{ marginTop: 0 }}>
        Scanned before the build, because building installs dependencies and
        that runs their code. Tools: {scan.tools_run.join(", ")}
        {Object.keys(scan.tools_skipped).length > 0 && (
          <>
            {" · unavailable: "}
            {Object.entries(scan.tools_skipped)
              .map(([tool, why]) => `${tool} (${why})`)
              .join(", ")}
          </>
        )}
      </p>

      {scan.findings.length === 0 ? (
        <div className="card">Nothing flagged.</div>
      ) : (
        <div className="card">
          {scan.findings.map((f, i) => (
            <div className="finding" key={`${f.rule}-${f.file}-${f.line}-${i}`}>
              <SeverityChip severity={f.severity} />
              <div className="body">
                <div>{f.message}</div>
                <div className="where">
                  {f.file}:{f.line} · {f.rule} · {f.tool}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
