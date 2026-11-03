/** Same key as persisted chat tabs in the workspace — single source for reads/writes/clear. */
export const CHAT_TABS_LOCAL_STORAGE_KEY = "chief-of-staff.chat-workspace-tabs.v1";

/**
 * Conversations whose local transcript is known to be behind the server.
 *
 * A client that leaves mid-stream has only a partial reply locally, while the
 * backend keeps generating and commits the full turn. Patching the partial
 * transcript back would truncate the saved conversation, so the id is parked
 * here and the thread is re-read from the server on return.
 */
const NEEDS_REFRESH_LOCAL_STORAGE_KEY = "chief-of-staff.chat-needs-refresh.v1";

function readNeedsRefreshIds(): Set<string> {
  if (typeof window === "undefined") {
    return new Set();
  }
  try {
    const raw = window.localStorage.getItem(NEEDS_REFRESH_LOCAL_STORAGE_KEY);
    if (!raw) {
      return new Set();
    }
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return new Set();
    }
    return new Set(parsed.filter((id): id is string => typeof id === "string"));
  } catch {
    return new Set();
  }
}

function writeNeedsRefreshIds(ids: Set<string>): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    if (ids.size === 0) {
      window.localStorage.removeItem(NEEDS_REFRESH_LOCAL_STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(NEEDS_REFRESH_LOCAL_STORAGE_KEY, JSON.stringify([...ids]));
  } catch {
    // Private mode or blocked storage — refresh is just skipped.
  }
}

export function markConversationNeedsRefresh(conversationId: string): void {
  const ids = readNeedsRefreshIds();
  ids.add(conversationId);
  writeNeedsRefreshIds(ids);
}

export function clearConversationNeedsRefresh(conversationId: string): void {
  const ids = readNeedsRefreshIds();
  if (!ids.delete(conversationId)) {
    return;
  }
  writeNeedsRefreshIds(ids);
}

export function conversationNeedsRefresh(conversationId: string): boolean {
  return readNeedsRefreshIds().has(conversationId);
}

export function clearPersistedChatWorkspaceTabs(): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(CHAT_TABS_LOCAL_STORAGE_KEY);
  } catch {
    // Private mode or blocked storage
  }
}
