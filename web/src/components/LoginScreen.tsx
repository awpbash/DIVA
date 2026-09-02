import { useEffect, useState } from "react";
import { apiUrl } from "../api";
import { useAuth } from "../auth";
import { useBranding } from "../branding";
import "./LoginScreen.css";

// Passwordless login. Type an email that has an account and the access level
// comes with it. No password, which is a fine posture on your own machine and
// not one for a public address.
//
// There is deliberately no account picker: an email address IS the credential
// here, so a list of accounts is a list of ways in. The one exception is a
// brand-new instance, which the server tells us about via `signInHint`. Its
// operator has no way to know the address their own install just created, and
// that is a locked door with the key in the documentation. The server stops
// sending the hint the moment anyone signs in.

export function LoginScreen() {
  const { login } = useAuth();
  const { appName, signInHint } = useBranding();
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [server, setServer] = useState<"checking" | "warming" | "ready">("checking");

  // Warm-up poll: /readyz pings the knowledge store, so the first request
  // wakes the whole chain while the user is still typing their email.
  // Polling until it answers lets the card show an honest status instead of
  // leaving a cold click to fail. A freshly started database container is the
  // usual reason it is not ready yet; the app creates its own database at boot
  // and only has to wait for the container to come up.
  useEffect(() => {
    let stop = false;
    (async () => {
      for (let round = 0; round < 40 && !stop; round++) {
        try {
          const r = await fetch(apiUrl("/readyz"));
          if (r.ok) { if (!stop) setServer("ready"); return; }
          if (!stop) setServer("warming");
        } catch { if (!stop) setServer("warming"); }
        await new Promise(res => setTimeout(res, 3000));
      }
    })();
    return () => { stop = true; };
  }, []);

  async function doLogin(addr: string) {
    if (!addr.trim()) return;
    setBusy(true); setError(null);
    try { await login(addr.trim()); }
    catch (e) { setError((e as Error).message || "Login failed."); setBusy(false); }
  }

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <div className="login__mark" />
          <span>{appName}</span>
        </div>
        <h1>Sign in</h1>
        <p className="login__sub">Enter your email. Your access level is set by an administrator. This build is passwordless.</p>

        {signInHint && (
          <div className="login__firstrun">
            <b>First run.</b> This instance created one administrator account:
            <button
              type="button"
              className="login__hint-fill"
              onClick={() => setEmail(signInHint)}
              disabled={busy}
            >{signInHint}</button>
            Change it with <code>BOOTSTRAP_ADMIN_EMAIL</code> before first
            start, or add accounts once you are in. This notice disappears
            after the first sign-in.
          </div>
        )}

        <form className="login__form" onSubmit={e => { e.preventDefault(); doLogin(email); }}>
          <input
            type="email" placeholder="you@company.com" value={email} autoFocus
            onChange={e => setEmail(e.target.value)} disabled={busy}
          />
          <button type="submit" disabled={busy || !email.trim()}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        {error && <div className="login__error">{error}</div>}

        {server === "warming" && (
          <div className="login__server login__server--warming">
            <span className="login__server-dot" aria-hidden />
            Waiting for the knowledge store. A database container that has just
            started takes up to a minute to answer. If this does not clear, check
            that it is running: <code>docker compose up -d cosmos</code>.
          </div>
        )}
        {server === "ready" && (
          <div className="login__server login__server--ready">
            <span className="login__server-dot" aria-hidden />
            Server ready.
          </div>
        )}
      </div>
    </div>
  );
}
