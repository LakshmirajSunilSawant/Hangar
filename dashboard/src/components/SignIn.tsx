import { useState } from "react";
import { api, setToken } from "../api";
import type { WhoAmI } from "../types";

type Mode = "signin" | "invite" | "token";

/** Sign in, accept an invitation, or fall back to the admin API token. */
export function SignIn({ onSignedIn }: { onSignedIn: (who: WhoAmI) => void }) {
  const [mode, setMode] = useState<Mode>("signin");

  return (
    <div className="shell">
      <div className="gate">
        <h1 style={{ fontSize: 22, margin: "0 0 4px" }}>Hangar</h1>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Cloud for small software
        </p>

        {mode === "signin" && <SignInForm onSignedIn={onSignedIn} />}
        {mode === "invite" && <AcceptInviteForm onSignedIn={onSignedIn} />}
        {mode === "token" && <TokenForm onDone={() => onSignedIn(ADMIN_WHO)} />}

        <div className="row" style={{ marginTop: 16, fontSize: 13 }}>
          {mode !== "signin" && (
            <button className="link" onClick={() => setMode("signin")}>
              Sign in
            </button>
          )}
          {mode !== "invite" && (
            <button className="link" onClick={() => setMode("invite")}>
              I have an invitation
            </button>
          )}
          {mode !== "token" && (
            <button className="link" onClick={() => setMode("token")}>
              Use an API token
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

const ADMIN_WHO: WhoAmI = {
  kind: "admin",
  email: null,
  is_admin: true,
  authenticated: true,
};

function useSubmit(onSignedIn: (who: WhoAmI) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run(fn: () => Promise<WhoAmI>) {
    setBusy(true);
    setError("");
    try {
      onSignedIn(await fn());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return { busy, error, run };
}

function SignInForm({ onSignedIn }: { onSignedIn: (who: WhoAmI) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useSubmit(onSignedIn);

  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        void run(() => api.login(email, password));
      }}
    >
      <div className="field">
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
          autoFocus
        />
      </div>
      <div className="field">
        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
      </div>

      {error && <div className="error">{error}</div>}

      <button className="primary" type="submit" disabled={busy || !email || !password}>
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

function AcceptInviteForm({ onSignedIn }: { onSignedIn: (who: WhoAmI) => void }) {
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const { busy, error, run } = useSubmit(onSignedIn);

  const tooShort = password.length > 0 && password.length < 10;

  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        void run(() => api.acceptInvite(token.trim(), password));
      }}
    >
      <p className="hint" style={{ marginTop: 0 }}>
        Whoever invited you passed on a one-time code. Choose a password to
        finish setting up your account.
      </p>
      <div className="field">
        <label htmlFor="invite">Invitation code</label>
        <input
          id="invite"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="off"
          autoFocus
        />
      </div>
      <div className="field">
        <label htmlFor="new-password">Choose a password</label>
        <input
          id="new-password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
        />
        <div className="hint">At least 10 characters.</div>
      </div>

      {error && <div className="error">{error}</div>}

      <button
        className="primary"
        type="submit"
        disabled={busy || !token.trim() || tooShort || !password}
      >
        {busy ? "Setting up…" : "Create account"}
      </button>
    </form>
  );
}

function TokenForm({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");

  return (
    <form
      className="card"
      onSubmit={(e) => {
        e.preventDefault();
        setToken(value.trim());
        onDone();
      }}
    >
      <p className="hint" style={{ marginTop: 0 }}>
        The operator credential — <code>HANGAR_API_TOKEN</code> on the server.
        It grants full access to every app, so prefer signing in if you have an
        account.
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
      </div>
      <button className="primary" type="submit" disabled={!value.trim()}>
        Continue
      </button>
    </form>
  );
}
