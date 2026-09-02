import { useEffect, useState } from "react";
import { apiWarmingSince, isApiWarming } from "../api";
import "./WarmupBanner.css";

/** Calm notice while the sleeping demo server cold-starts. Driven by the
 * "api:warming" events apiFetch() broadcasts: appears the moment a request
 * hits a waking server (or silently hangs on one), clears on the first
 * response that gets through. Shows an honest elapsed count and staged copy
 * instead of a fake percentage — the wake usually finishes inside 15s. */
export function WarmupBanner() {
  const [warming, setWarming] = useState(isApiWarming());
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const on = (e: Event) => setWarming(Boolean((e as CustomEvent).detail));
    window.addEventListener("api:warming", on);
    return () => window.removeEventListener("api:warming", on);
  }, []);

  useEffect(() => {
    if (!warming) { setElapsed(0); return; }
    const tick = () => {
      const since = apiWarmingSince();
      setElapsed(since ? Math.max(0, Math.floor((Date.now() - since) / 1000)) : 0);
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [warming]);

  if (!warming) return null;
  const note =
    elapsed >= 45
      ? "Taking longer than usual. Nothing is lost, requests keep retrying on their own."
      : elapsed >= 18
        ? "Almost there. A first start can take a little longer."
        : "This usually takes under 15 seconds.";
  return (
    <div className="warmup" role="status">
      <div className="warmup__row">
        <span className="warmup__spin" aria-hidden />
        <div className="warmup__text">
          <strong>Starting the server</strong>
          <span>
            The app sleeps when nobody is using it, to keep hosting costs low.
            It is waking up now. {note}
          </span>
        </div>
        <span className="warmup__elapsed">{elapsed}s</span>
      </div>
      <div className="warmup__bar" aria-hidden><span /></div>
    </div>
  );
}
