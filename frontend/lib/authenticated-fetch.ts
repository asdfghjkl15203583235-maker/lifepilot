import { mergeAuthHeaders } from "@/lib/auth-session";

const DEFAULT_TIMEOUT_MS = 30_000;

export interface AuthenticatedFetchInit extends RequestInit {
  /** Abort after this many ms. 0 disables the timeout (for long SSE streams like /chat). */
  timeout?: number;
}

function timeoutSignal(
  initSignal: AbortSignal | undefined | null,
  ms: number,
): { signal: AbortSignal | undefined; clearTimeout: () => void } {
  if (typeof AbortSignal.timeout === "function") {
    const withTimeout = initSignal ? AbortSignal.any([initSignal, AbortSignal.timeout(ms)]) : AbortSignal.timeout(ms);
    return { signal: withTimeout, clearTimeout: () => undefined };
  }
  if (!initSignal) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(new Error("请求超时")), ms);
    return {
      signal: controller.signal,
      clearTimeout: () => clearTimeout(timer),
    };
  }
  const controller = new AbortController();
  const onAbort = () => controller.abort(initSignal.reason);
  initSignal.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(() => controller.abort(new Error("请求超时")), ms);
  return {
    signal: controller.signal,
    clearTimeout: () => {
      clearTimeout(timer);
      initSignal.removeEventListener("abort", onAbort);
    },
  };
}

/** API calls with credentials + Bearer token when stored (cross-origin prod).
 *  Aborts after 30s so a hung backend surfaces as an error (with retry UI) instead of
 *  a permanently pending request.  Caller-provided signals are merged so tab close /
 *  conversation abort still cancels the request. */
export function authenticatedFetch(
  input: RequestInfo | URL,
  init?: AuthenticatedFetchInit,
): Promise<Response> {
  const { timeout = DEFAULT_TIMEOUT_MS, ...rest } = init ?? {};
  const headers = mergeAuthHeaders(rest.headers);
  const { signal, clearTimeout } =
    timeout > 0 ? timeoutSignal(rest.signal, timeout) : { signal: rest.signal, clearTimeout: () => undefined };
  return fetch(input, {
    ...rest,
    credentials: "include",
    headers,
    signal,
  }).finally(clearTimeout);
}
