import { useState } from "react";
import { api } from "../api";
import type { App, SourceType } from "../types";

/** The three ways source gets into Hangar, behind one form. */
export function NewApp({
  onCreated,
  onCancel,
}: {
  onCreated: (app: App) => void;
  onCancel: () => void;
}) {
  const [source, setSource] = useState<SourceType>("repo");
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const ready =
    name.trim() !== "" &&
    ((source === "path" && path.trim() !== "") ||
      (source === "repo" && repo.trim() !== "") ||
      (source === "zip" && file !== null));

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const app =
        source === "path" ? await api.fromPath(name, path)
        : source === "repo" ? await api.fromRepo(name, repo, ref)
        : await api.fromZip(name, file!);
      onCreated(app);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card" onSubmit={submit}>
      <div className="spread" style={{ marginBottom: 16 }}>
        <strong>Deploy an app</strong>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>

      <div className="field">
        <label htmlFor="source">Source</label>
        <select
          id="source"
          value={source}
          onChange={(e) => setSource(e.target.value as SourceType)}
        >
          <option value="repo">GitHub repository</option>
          <option value="zip">Upload a zip</option>
          <option value="path">Directory on the server</option>
        </select>
      </div>

      <div className="field">
        <label htmlFor="name">Name</label>
        <input
          id="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="team-dashboard"
          autoComplete="off"
        />
        <div className="hint">
          Lowercase letters, digits and hyphens. Becomes the app's hostname.
        </div>
      </div>

      {source === "repo" && (
        <>
          <div className="field">
            <label htmlFor="repo">Repository</label>
            <input
              id="repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="https://github.com/owner/repo"
              autoComplete="off"
            />
            <div className="hint">A full URL or just owner/repo.</div>
          </div>
          <div className="field">
            <label htmlFor="ref">Branch, tag or commit (optional)</label>
            <input
              id="ref"
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="main"
              autoComplete="off"
            />
            <div className="hint">
              Redeploying re-fetches, so it always picks up the latest commit.
            </div>
          </div>
        </>
      )}

      {source === "zip" && (
        <div className="field">
          <label htmlFor="file">Zip archive</label>
          <input
            id="file"
            type="file"
            accept=".zip,application/zip"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
          <div className="hint">
            The app's source, not a built image. Max 100 MB.
          </div>
        </div>
      )}

      {source === "path" && (
        <div className="field">
          <label htmlFor="path">Absolute path</label>
          <input
            id="path"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/srv/apps/my-tool"
            autoComplete="off"
          />
          <div className="hint">
            A directory on the machine running Hangar, not on your computer.
          </div>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      <button className="primary" type="submit" disabled={!ready || busy}>
        {busy ? "Deploying…" : "Deploy"}
      </button>
    </form>
  );
}
