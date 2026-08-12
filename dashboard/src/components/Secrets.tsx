/** Environment variables an app needs but must not ship in its source.
 *
 * The UI states plainly that values cannot be read back, because a form that
 * shows names and blank value fields otherwise reads as "the value is empty"
 * rather than "the value is not disclosed".
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Secret } from "../types";

export function Secrets({ appId, onChanged }: { appId: string; onChanged?: () => void }) {
  const [secrets, setSecrets] = useState<Secret[] | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const load = useCallback(async () => {
    try {
      setSecrets(await api.secrets(appId));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [appId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.putSecret(appId, name.trim(), value);
      setSaved(name.trim());
      // Clear the value immediately: it cannot be fetched again, and leaving
      // it in the DOM is the one place it would linger.
      setName("");
      setValue("");
      await load();
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(secretName: string) {
    if (!confirm(`Delete ${secretName}? The app loses it on its next deploy.`)) return;
    try {
      await api.deleteSecret(appId, secretName);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div>
      <p className="hint" style={{ marginTop: 0 }}>
        Injected into the container as environment variables, encrypted at rest,
        and <strong>never readable back</strong> — not here, not through the API.
        Changes take effect on the next deploy.
      </p>

      <form className="card" onSubmit={submit} style={{ marginBottom: 16 }}>
        <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
          <input
            placeholder="API_KEY"
            value={name}
            onChange={(e) => setName(e.target.value.toUpperCase())}
            required
            pattern="[A-Z][A-Z0-9_]*"
            title="Capitals, digits and underscores, starting with a letter"
            style={{ flex: "1 1 180px" }}
          />
          <input
            type="password"
            placeholder="value"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            required
            autoComplete="new-password"
            style={{ flex: "2 1 240px" }}
          />
          <button className="primary" disabled={busy || !name || !value}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
        {saved && !error && (
          <p className="muted" style={{ margin: "8px 0 0", fontSize: 13 }}>
            Saved <span className="mono">{saved}</span>. Redeploy the app for it
            to take effect.
          </p>
        )}
      </form>

      {error && <div className="error">{error}</div>}

      {secrets === null ? (
        <p className="muted">Loading…</p>
      ) : secrets.length === 0 ? (
        <div className="card empty">
          <p style={{ margin: 0 }}>No secrets set.</p>
        </div>
      ) : (
        <div className="card">
          {secrets.map((secret) => (
            <div className="finding" key={secret.name}>
              <div className="body">
                <div className="mono">{secret.name}</div>
                <div className="where">
                  set {new Date(secret.updated_at).toLocaleString()}
                </div>
              </div>
              <button className="danger" onClick={() => void remove(secret.name)}>
                Delete
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
