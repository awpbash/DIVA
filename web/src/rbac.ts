// Which tabs each role may use — the STATIC FALLBACK. The server is the source
// of truth (`capabilities.tabs` on /auth/login and /auth/me, from
// api/routes/auth.py); this mirror only covers the gap while a session
// restores. Keep the two in sync. Note: the approved-verifier flag also opens
// the "review" tab for non-admins — that is server-only (per-account, not
// per-role), so it can't be mirrored here; capabilities.tabs carries it.

export const TABS_FOR_ROLE: Record<string, string[]> = {
  admin:        ["chat", "explore", "review", "knowledge", "ontology", "admin"],
  confidential: ["chat", "explore", "knowledge"],
  default:      ["chat"],
};

export function tabsForRole(role: string | null | undefined): string[] {
  return TABS_FOR_ROLE[role ?? ""] ?? TABS_FOR_ROLE.default;
}
