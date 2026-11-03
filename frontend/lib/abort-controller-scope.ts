/**
 * Per-conversation sets of AbortControllers so many requests can run in parallel
 * and each can be removed when it finishes without affecting siblings.
 */
export function addControllerToScopeSet(
  byScope: Map<string, Set<AbortController>>,
  scopeId: string,
  controller: AbortController,
): void {
  let set = byScope.get(scopeId);
  if (set === undefined) {
    set = new Set();
    byScope.set(scopeId, set);
  }
  set.add(controller);
}

export function removeControllerFromScopeSet(
  byScope: Map<string, Set<AbortController>>,
  scopeId: string,
  controller: AbortController,
): void {
  const set = byScope.get(scopeId);
  if (set === undefined) {
    return;
  }
  set.delete(controller);
  if (set.size === 0) {
    byScope.delete(scopeId);
  }
}

export function abortAllControllersInScope(
  byScope: Map<string, Set<AbortController>>,
  scopeId: string,
): void {
  const set = byScope.get(scopeId);
  if (set === undefined) {
    return;
  }
  for (const c of set) {
    c.abort();
  }
  byScope.delete(scopeId);
}

export function clearMapAndAbortAllControllers(byScope: Map<string, Set<AbortController>>): void {
  for (const set of byScope.values()) {
    for (const c of set) {
      c.abort();
    }
  }
  byScope.clear();
}
