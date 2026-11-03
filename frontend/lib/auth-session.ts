/** Session JWT for cross-origin API calls when cookies are not sent (SameSite + separate ALB hosts). */

const SESSION_STORAGE_KEY = "chief_of_staff_api_session";

const OAUTH_FRAGMENT_PREFIX = "#session=";

export function consumeOAuthFragment(): void {
  if (typeof window === "undefined") return;
  const { hash } = window.location;
  if (!hash.startsWith(OAUTH_FRAGMENT_PREFIX)) return;
  const encoded = hash.slice(OAUTH_FRAGMENT_PREFIX.length);
  try {
    const token = decodeURIComponent(encoded);
    if (token) {
      sessionStorage.setItem(SESSION_STORAGE_KEY, token);
    }
  } finally {
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
}

export function clearSessionToken(): void {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem(SESSION_STORAGE_KEY);
}

export function mergeAuthHeaders(initHeaders?: HeadersInit): Headers {
  const headers = new Headers(initHeaders ?? undefined);
  if (typeof window !== "undefined") {
    const token = sessionStorage.getItem(SESSION_STORAGE_KEY);
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
  }
  return headers;
}
