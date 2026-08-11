import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { ROLES, ROLE_SUMMARY, type Grant, type Role } from "../types";

/** Who can reach an app, and what they may do — the "share it" panel. */
export function Sharing({ appId }: { appId: string }) {
  const [grants, setGrants] = useState<Grant[] | null>(null);
  const [error, setError] = useState("");
  const [denied, setDenied] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setGrants(await api.access(appId));
      setError("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setDenied(true);
      else setError(err instanceof Error ? err.message : String(err));
    }
  }, [appId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (denied) {
    return (
      <p className="muted">
        Only an owner of this app can see or change who has access.
      </p>
    );
  }

  return (
    <div>
      <GrantForm appId={appId} onGranted={refresh} />

      {error && <div className="error">{error}</div>}

      {grants === null ? (
        <p className="muted">Loading…</p>
      ) : grants.length === 0 ? (
        <div className="card">
          Nobody else has access yet. Anyone you add signs in to Hangar — the
          app itself never sees a password.
        </div>
      ) : (
        <div className="card">
          {grants.map((grant) => (
            <GrantRow
              key={grant.user_id}
              appId={appId}
              grant={grant}
              onChanged={refresh}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function GrantRow({
  appId,
  grant,
  onChanged,
}: {
  appId: string;
  grant: Grant;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="finding" style={{ alignItems: "center" }}>
      <div className="body">
        <div>{grant.email}</div>
        <div className="where">{grant.can.join(" · ")}</div>
        {error && <div className="error">{error}</div>}
      </div>

      <select
        value={grant.role}
        disabled={busy}
        aria-label={`Role for ${grant.email}`}
        style={{ width: "auto" }}
        onChange={(e) =>
          act(() => api.grant(appId, grant.email, e.target.value as Role))
        }
      >
        {ROLES.map((role) => (
          <option key={role} value={role}>
            {role}
          </option>
        ))}
      </select>

      <button
        className="danger"
        disabled={busy}
        onClick={() => {
          if (confirm(`Remove ${grant.email}'s access to this app?`))
            void act(() => api.revoke(appId, grant.user_id));
        }}
      >
        Remove
      </button>
    </div>
  );
}

function GrantForm({
  appId,
  onGranted,
}: {
  appId: string;
  onGranted: () => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [invite, setInvite] = useState<{ email: string; token: string } | null>(
    null,
  );

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setInvite(null);
    try {
      await api.grant(appId, email.trim(), role);
      setEmail("");
      onGranted();
    } catch (err) {
      // The API refuses to grant access to an address with no account, so a
      // typo can't hand access to whoever registers it later. Offer to invite.
      if (err instanceof ApiError && err.status === 404) {
        setError(`${email.trim()} doesn't have an account yet.`);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function inviteThem() {
    setBusy(true);
    try {
      const created = await api.inviteUser(email.trim());
      await api.grant(appId, email.trim(), role);
      setInvite({ email: created.user.email, token: created.invite_token });
      setError("");
      setEmail("");
      onGranted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form className="card" onSubmit={submit} style={{ marginBottom: 12 }}>
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field grow" style={{ flex: 1, marginBottom: 0, minWidth: 200 }}>
            <label htmlFor="share-email">Share with</label>
            <input
              id="share-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="teammate@example.com"
              autoComplete="off"
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor="share-role">Role</label>
            <select
              id="share-role"
              value={role}
              onChange={(e) => setRole(e.target.value as Role)}
              style={{ width: "auto" }}
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
          <button className="primary" type="submit" disabled={busy || !email.trim()}>
            Share
          </button>
        </div>
        <div className="hint">{ROLE_SUMMARY[role]}</div>

        {error && (
          <div className="error" style={{ marginBottom: 0 }}>
            {error}{" "}
            {error.includes("doesn't have an account") && (
              <button className="link" type="button" onClick={inviteThem} disabled={busy}>
                Invite them
              </button>
            )}
          </div>
        )}
      </form>

      {invite && (
        <div className="card" style={{ marginBottom: 12 }}>
          <strong>Invitation for {invite.email}</strong>
          <p className="hint" style={{ marginTop: 4 }}>
            Hangar sends no email. Pass this one-time code to them — they enter
            it under “I have an invitation”.
          </p>
          <pre className="log" style={{ maxHeight: "none", marginBottom: 8 }}>
            {invite.token}
          </pre>
          <button onClick={() => navigator.clipboard?.writeText(invite.token)}>
            Copy code
          </button>
        </div>
      )}
    </>
  );
}
