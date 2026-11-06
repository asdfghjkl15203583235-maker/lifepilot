"use client";

import { useCallback, useRef } from "react";

import {
  addControllerToScopeSet,
  removeControllerFromScopeSet,
  abortAllControllersInScope,
} from "@/lib/abort-controller-scope";
import { authenticatedFetch } from "@/lib/authenticated-fetch";
import { markConversationNeedsRefresh } from "@/lib/chat-workspace-storage";
import type { PendingActionRecord } from "@/lib/pending-actions";

const DEFAULT_API_BASE_URL = "http://localhost:8081";

/** A dropped stream is resumed at most this many times before surfacing an error. */
const MAX_STREAM_RECONNECTS = 3;
const RECONNECT_DELAY_MS = 600;

/** /chat/cancel 是"尽力而为"的通知，卡住不能拖住 UI。 */
const CANCEL_REQUEST_TIMEOUT_MS = 5000;

type PendingActionType = PendingActionRecord["actionType"];
type PendingActionStatus = PendingActionRecord["status"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function coercePendingAction(value: unknown): PendingActionRecord | null {
  if (!isRecord(value)) {
    return null;
  }
  const id = typeof value.id === "string" ? value.id : "";
  if (!id) {
    return null;
  }
  const actionType = String(value.actionType ?? "") as PendingActionType;
  const validTypes: PendingActionType[] = ["email_send", "email_reply", "task_update", "task_delete"];
  if (!validTypes.includes(actionType)) {
    return null;
  }
  const status = String(value.status ?? "pending") as PendingActionStatus;
  return {
    id,
    actionType,
    status,
    title: typeof value.title === "string" ? value.title : "待确认操作",
    summary: typeof value.summary === "string" ? value.summary : null,
    payload: isRecord(value.payload) ? value.payload : {},
    createdAt: typeof value.createdAt === "string" ? value.createdAt : null,
    expiresAt: typeof value.expiresAt === "string" ? value.expiresAt : null,
  };
}

export interface ChatCompletionMessagePayload {
  role: "system" | "user" | "assistant";
  content: string;
  /** ISO timestamp of when the message was created — lets the backend isolate the latest request. */
  timestamp?: string;
}

interface CompletionHandlers {
  onSuccess: (assistantText: string, trace?: string) => void;
  onComplete: () => void;
  onError: (message: string) => void;
  /** Fired when this request is aborted (tab closed or workspace unmount). */
  onAbort?: () => void;
  /** Fired each time the backend emits a progress status update. */
  onStatus?: (message: string) => void;
  /** Fired each time the backend streams a reply token delta. */
  onToken?: (content: string) => void;
  /** Fired when the backend emits a HITL confirmation draft (before done). */
  onPendingAction?: (action: PendingActionRecord) => void;
}

type StreamOutcome = "done" | "error" | "dropped" | "resync";

interface StreamResult {
  outcome: StreamOutcome;
  /** id of the last SSE event consumed, replayed as Last-Event-ID on reconnect. */
  lastEventId: number | null;
}

function getApiBaseUrl(explicit?: string): string {
  const fromEnv = process.env.NEXT_PUBLIC_API_BASE_URL;
  const base = explicit ?? (typeof fromEnv === "string" && fromEnv.length > 0 ? fromEnv : DEFAULT_API_BASE_URL);
  return base.replace(/\/$/, "");
}

function parseEventId(rawEvent: string): number | null {
  for (const line of rawEvent.split("\n")) {
    if (!line.startsWith("id:")) continue;
    const value = Number.parseInt(line.slice(3).trim(), 10);
    return Number.isFinite(value) ? value : null;
  }
  return null;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Many parallel /chat requests per tab; closing a tab calls `abortConversation` to cancel all for that scope.
 */
export function useChatCompletion(apiBaseUrl?: string) {
  const abortControllersByScopeRef = useRef<Map<string, Set<AbortController>>>(new Map());

  const abortConversation = useCallback((conversationScopeId: string) => {
    abortAllControllersInScope(abortControllersByScopeRef.current, conversationScopeId);
  }, []);

  /**
   * 明确告诉后端「这一轮不要了」。
   *
   * `abort()` 只断开连接，后端分不清是用户点停止还是网络抖动，会白跑一整轮
   * 生成；这个请求把意图显式送过去，后端立刻取消 LangGraph 并释放资源。
   * 故意不传 signal：它必须活过即将发生的 abort，否则连取消消息都发不出去。
   */
  const cancelConversation = useCallback(
    async (conversationScopeId: string): Promise<void> => {
      try {
        await authenticatedFetch(`${getApiBaseUrl(apiBaseUrl)}/chat/cancel`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_conversation_id: conversationScopeId }),
          timeout: CANCEL_REQUEST_TIMEOUT_MS,
        });
      } catch (error) {
        // 取消失败不该拦住本地 abort：本地已经停下，服务端最多多跑一轮。
        console.warn("取消生成请求失败：", error);
      }
    },
    [apiBaseUrl],
  );

  /**
   * One POST /chat stream attempt. Returns how it ended plus the last seen event
   * id, so the caller can resume with `Last-Event-ID` instead of replaying the turn.
   */
  const readStream = useCallback(
    async (
      baseUrl: string,
      conversationScopeId: string,
      messages: ChatCompletionMessagePayload[],
      handlers: CompletionHandlers,
      controller: AbortController,
      lastEventId: number | null,
    ): Promise<StreamResult> => {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      };
      if (lastEventId !== null) {
        headers["Last-Event-ID"] = String(lastEventId);
      }

      const response = await authenticatedFetch(`${baseUrl}/chat`, {
        method: "POST",
        headers,
        body: JSON.stringify({ messages, client_conversation_id: conversationScopeId }),
        signal: controller.signal,
        timeout: 0, // SSE stream — an agent run can exceed the default 30s timeout.
      });

      if (!response.ok) {
        const detail = await response.text();
        handlers.onError(detail.length > 0 ? detail : response.statusText);
        return { outcome: "error", lastEventId };
      }

      const reader = response.body?.getReader();
      if (!reader) {
        handlers.onError("对话接口返回空响应");
        return { outcome: "error", lastEventId };
      }

      const decoder = new TextDecoder();
      let buffer = "";
      let cursor = lastEventId;

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });

          // SSE events are delimited by double newlines
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";

          for (const rawEvent of frames) {
            const eventId = parseEventId(rawEvent);
            if (eventId !== null) {
              cursor = eventId;
            }

            const dataLine = rawEvent.split("\n").find((l) => l.startsWith("data: "));
            if (!dataLine) continue;

            let parsed: Record<string, unknown>;
            try {
              parsed = JSON.parse(dataLine.slice(6)) as Record<string, unknown>;
            } catch {
              continue;
            }

            const type = parsed.type;

            if (type === "status" && typeof parsed.message === "string") {
              handlers.onStatus?.(parsed.message);
            } else if (type === "token" && typeof parsed.content === "string") {
              handlers.onToken?.(parsed.content);
            } else if (type === "done") {
              const content = parsed.content;
              if (typeof content !== "string") {
                handlers.onError("对话接口返回无效响应");
                return { outcome: "error", lastEventId: cursor };
              }
              const trace = typeof parsed.trace === "string" ? parsed.trace : undefined;
              handlers.onSuccess(content, trace);
              handlers.onComplete();
              return { outcome: "done", lastEventId: cursor };
            } else if (type === "cancelled") {
              // 服务端确认这一轮已取消：正常收尾，不要当成断线去重连。
              handlers.onAbort?.();
              return { outcome: "done", lastEventId: cursor };
            } else if (type === "pending_action") {
              const action = coercePendingAction(parsed.action);
              if (action !== null) {
                handlers.onPendingAction?.(action);
              }
            } else if (type === "error") {
              // The backend no longer holds this conversation's buffer; the turn
              // was almost certainly committed, so park the thread for a server
              // resync instead of re-running (and double-booking) the turn.
              if (parsed.resync === true) {
                markConversationNeedsRefresh(conversationScopeId);
                handlers.onError(
                  typeof parsed.message === "string"
                    ? parsed.message
                    : "连接已过期，请刷新页面查看已保存的回复。",
                );
                return { outcome: "resync", lastEventId: cursor };
              }
              if (typeof parsed.message === "string") {
                handlers.onError(parsed.message);
                return { outcome: "error", lastEventId: cursor };
              }
            }
          }
        }
      } finally {
        reader.releaseLock();
      }

      // Stream closed without a terminal event: connection dropped mid-turn.
      return { outcome: "dropped", lastEventId: cursor };
    },
    [],
  );

  const requestAssistantReply = useCallback(
    async (
      conversationScopeId: string,
      messages: ChatCompletionMessagePayload[],
      handlers: CompletionHandlers,
    ): Promise<void> => {
      const byScope = abortControllersByScopeRef.current;
      const controller = new AbortController();
      addControllerToScopeSet(byScope, conversationScopeId, controller);
      const baseUrl = getApiBaseUrl(apiBaseUrl);

      try {
        let lastEventId: number | null = null;
        for (let attempt = 0; attempt <= MAX_STREAM_RECONNECTS; attempt += 1) {
          if (attempt > 0) {
            await delay(RECONNECT_DELAY_MS * attempt);
          }
          const result = await readStream(
            baseUrl,
            conversationScopeId,
            messages,
            handlers,
            controller,
            lastEventId,
          );
          if (result.outcome === "done" || result.outcome === "error" || result.outcome === "resync") {
            return;
          }
          lastEventId = result.lastEventId;
        }
        handlers.onError("响应完成前连接已关闭");
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          handlers.onAbort?.();
          return;
        }
        const message = error instanceof Error ? error.message : "请求失败";
        handlers.onError(message);
      } finally {
        removeControllerFromScopeSet(abortControllersByScopeRef.current, conversationScopeId, controller);
      }
    },
    [apiBaseUrl, readStream],
  );

  // 这里刻意不做"卸载即 abort"。切页会卸载整个工作区，若在此中止 SSE，后端
  // 在 DISCONNECT_CANCEL_GRACE_SECONDS（3 秒）内等不到重连就会取消这一轮，并把
  // 已生成的内容随事务一起回滚，用户回来只剩自己那条提问。保持连接让服务端跑完
  // 并落库，回来时由 needsRefresh 补拉。"停止生成"走 cancelConversation，不依赖这里。

  return { requestAssistantReply, abortConversation, cancelConversation };
}
