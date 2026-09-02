// Tiny sessionStorage-backed helpers for lightweight surface state (selected
// document, drill-in position, chat draft). Tab components unmount on every
// tab switch, so plain useState loses the user's place. These keep it for the
// session without touching the server. Values are JSON so null round-trips.

export function loadUi<T>(key: string, fallback: T): T {
  try {
    const raw = sessionStorage.getItem(`ui.${key}`);
    return raw == null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function saveUi<T>(key: string, value: T): void {
  try {
    sessionStorage.setItem(`ui.${key}`, JSON.stringify(value));
  } catch {
    // Storage can be unavailable (private mode). Losing the hint is fine.
  }
}
