import { DocumentMeta } from "./types";

// Display helpers for document metadata. Stored titles are filenames
// ("3 - [Supplementary Agreement] Second Supplementary…") — filing junk that
// makes bad UI labels. Every picker/list cleans + sorts through here so the
// whole app presents documents the same way: grouped by contract family,
// oldest first (base contract at the top, amendments in chain order).

/** Strip filing junk from a stored title: leading "3 - " / "4- " / "3 ~ "
 * numbering, bracketed "[Supplementary Agreement]" tags, trailing " p1". */
export function cleanTitle(raw: string): string {
  const t = (raw || "")
    .replace(/^\s*\d+\s*[-~—]\s*/, "")
    .replace(/\[[^\]]*\]\s*/g, "")
    .replace(/\s+p\d+\s*$/i, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  return t || raw;
}

/** Short label for the stated document type, to fit a one-line meta row.
 *
 * A lookup table of six type names used to sit here, taken from an older
 * revision of one domain's schema. It matched nothing either shipped domain
 * declares, so it always fell through to the raw string. Rather than replace
 * one domain's list with another's, shorten generically: a type name is
 * whatever the domain's `document_family.document_types` says it is, and the
 * only thing the UI actually needs is for a long one not to wrap. */
const SHORT_MAX = 18;

export function shortType(t: string | null | undefined): string | null {
  if (!t) return null;
  const trimmed = t.trim();
  if (trimmed.length <= SHORT_MAX) return trimmed;
  // Prefer cutting at a word boundary so "Amended and Restated" does not
  // become "Amended and Rest…".
  const cut = trimmed.lastIndexOf(" ", SHORT_MAX);
  return (cut > SHORT_MAX / 2 ? trimmed.slice(0, cut) : trimmed.slice(0, SHORT_MAX)) + "…";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
/** ISO date → compact display ("2022-10-17" → "17 Oct 2022"). */
export function shortDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!m) return iso;
  return `${parseInt(m[3], 10)} ${MONTHS[parseInt(m[2], 10) - 1]} ${m[1]}`;
}

/** The pseudo-folder for documents that belong to no folder. One label
 * everywhere (admin, catalog, review) so the drill-in reads the same. */
export const STANDALONE = "Loose documents";

export function familyOf(d: DocumentMeta): string {
  return d.group_name?.trim() || d.group?.trim() || STANDALONE;
}

/** Family (named families alphabetical, standalone last) → date (undated last) → title. */
export function sortDocs(docs: DocumentMeta[]): DocumentMeta[] {
  return [...docs].sort((a, b) => {
    const fa = familyOf(a), fb = familyOf(b);
    if (fa !== fb) {
      if (fa === STANDALONE) return 1;
      if (fb === STANDALONE) return -1;
      return fa.localeCompare(fb);
    }
    const da = a.doc_date_iso ?? "9999", db = b.doc_date_iso ?? "9999";
    if (da !== db) return da < db ? -1 : 1;
    return cleanTitle(a.title).localeCompare(cleanTitle(b.title));
  });
}

/** Sorted docs bucketed into ordered family sections. */
export function groupDocs(docs: DocumentMeta[]): { family: string; docs: DocumentMeta[] }[] {
  const out: { family: string; docs: DocumentMeta[] }[] = [];
  for (const d of sortDocs(docs)) {
    const fam = familyOf(d);
    if (!out.length || out[out.length - 1].family !== fam) {
      out.push({ family: fam, docs: [] });
    }
    out[out.length - 1].docs.push(d);
  }
  return out;
}
