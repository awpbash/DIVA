import { useEffect, useState } from "react";
import { authHeaders, notifyAuthExpired } from "../api";

// An <img> that can pass the session token. Plain <img src> sends no headers,
// so the login-gated page-image routes (/review/page, /km/page) would 401 —
// this fetches the image with auth and renders it from an object URL.

interface Props extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, "src"> {
  src: string;
}

export function AuthedImage({ src, alt, ...rest }: Props) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    let objectUrl: string | null = null;
    setUrl(null);
    fetch(src, { headers: authHeaders() })
      .then(r => {
        if (r.status === 401) { notifyAuthExpired(); throw new Error("401"); }
        if (!r.ok) throw new Error(String(r.status));
        return r.blob();
      })
      .then(b => {
        if (!alive) return;
        objectUrl = URL.createObjectURL(b);
        setUrl(objectUrl);
      })
      .catch(() => { /* broken page image — the alt text shows */ });
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  if (!url) return <div className="authed-img__loading" aria-label={alt} />;
  return <img src={url} alt={alt} {...rest} />;
}
