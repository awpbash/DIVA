/**
 * Instance branding, fetched once from GET /branding.
 *
 * The deployment names itself in the environment (APP_NAME / APP_TAGLINE), the
 * API serves that, and every screen reads it from here. Nothing in the frontend
 * hardcodes a product name, so renaming an instance is a restart rather than a
 * rebuild. The defaults below are what an unreachable API falls back to, which
 * matters on the login screen: it renders before any session exists.
 */
import { useEffect, useState } from "react";

import { apiFetch, apiUrl, authHeaders } from "./api";

export interface Branding {
  appName: string;
  tagline: string;
  /** Which document domain this instance is configured for. */
  domain: string;
  version: string;
  /** The address to sign in with, set ONLY while this instance has never been
   * signed into. It is how the operator of a fresh install learns the account
   * their own setup created. The server stops sending it after the first
   * successful sign-in, on any instance anyone has ever used. */
  signInHint: string | null;
  /** What an uploader may declare a document to BE, from the active domain's
   * schema. Served rather than written here: the menu is domain vocabulary. */
  documentTypes: string[];
  /** What this deployment CALLS the things it holds, singular and plural.
   * Every screen used to say "contract", which is wrong for a corpus of
   * medical records or planning applications and reads as somebody else's
   * product. Set with APP_DOCUMENT_NOUN / APP_DOCUMENT_NOUN_PLURAL. */
  docNoun: string;
  docNounPlural: string;
}

const FALLBACK: Branding = {
  appName: "DIVA",
  tagline: "Every answer traced to the clause it came from",
  domain: "",
  version: "",
  signInHint: null,
  documentTypes: [],
  docNoun: "document",
  docNounPlural: "documents",
};

// Module-level cache: the first component to mount pays for the request, every
// later mount renders the real name immediately with no flash.
let cached: Branding = FALLBACK;
let inflight: Promise<Branding> | null = null;

export function loadBranding(): Promise<Branding> {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const r = await apiFetch(apiUrl("/branding"), { headers: authHeaders() });
      if (r.ok) {
        const b = await r.json();
        cached = {
          appName: b.app_name || FALLBACK.appName,
          tagline: b.tagline || FALLBACK.tagline,
          domain: b.domain || "",
          version: b.version || "",
          signInHint: b.sign_in_hint || null,
          documentTypes: Array.isArray(b.document_types) ? b.document_types : [],
          docNoun: b.document_noun || FALLBACK.docNoun,
          docNounPlural: b.document_noun_plural || FALLBACK.docNounPlural,
        };
        document.title = cached.appName;
      }
    } catch {
      // Offline or still warming: keep the fallback, try again on next mount.
      inflight = null;
    }
    return cached;
  })();
  return inflight;
}

/** Current branding, synchronous. Returns the fallback until the fetch lands. */
export const branding = (): Branding => cached;

export function useBranding(): Branding {
  const [b, setB] = useState<Branding>(cached);
  useEffect(() => {
    let alive = true;
    loadBranding().then(next => { if (alive) setB(next); });
    return () => { alive = false; };
  }, []);
  return b;
}

/** The document types this instance's schema declares, for the upload form.
 *
 * Falls back to a generic set so the form is never unusable when the fetch has
 * not landed or an instance declares none. A domain names its own under
 * `document_family.document_types` in its field schema. */
export function useDocumentTypes(): string[] {
  const { documentTypes } = useBranding();
  return documentTypes.length ? documentTypes : FALLBACK_DOC_TYPES;
}

const FALLBACK_DOC_TYPES = [
  "Agreement", "Amendment", "Assignment", "Letter", "Notice", "Other",
];

/** What to call one of the things this instance holds, and several of them.
 *
 * Use it in any copy a user reads. Capitalise at the call site when the noun
 * starts a sentence: the server supplies it lowercase because that is how it
 * appears mid-sentence most of the time. */
export function useDocNoun(): { one: string; many: string } {
  const b = useBranding();
  return { one: b.docNoun, many: b.docNounPlural };
}

/** Capitalise the first letter, for a noun that opens a sentence. */
export const cap = (s: string): string =>
  s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
