"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export interface ConversationSummary {
  id: string;
  title: string | null;
  user_id: string;
  messages: unknown[];
  /** Unconfirmed HITL drafts for this thread (restored after a reload / tab switch). */
  pending_actions?: unknown[];
  created_at: string;
  updated_at: string;
}

const CONVERSATIONS_PATH = "/conversations";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // Keep the HTTP status text when the body is not JSON.
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

/**
 * CRUD wrapper for the authenticated conversation API. Callers own the
 * optimistic UI state and use `saveConversation` with their own debounce/queue.
 */
export function useConversations() {
  const requestIdRef = useRef(0);

  const listConversations = useCallback(async (): Promise<ConversationSummary[]> => {
    const data = await request<ConversationSummary[]>(CONVERSATIONS_PATH);
    return Array.isArray(data) ? data : [];
  }, []);

  const createConversation = useCallback(
    async (
      title: string | null,
      messages: unknown[],
      clientConversationId?: string,
    ): Promise<ConversationSummary> => {
      return request<ConversationSummary>(CONVERSATIONS_PATH, {
        method: "POST",
        body: JSON.stringify({
          title,
          messages,
          ...(clientConversationId ? { client_conversation_id: clientConversationId } : {}),
        }),
      });
    },
    [],
  );

  const getConversation = useCallback(async (conversationId: string): Promise<ConversationSummary | null> => {
    try {
      return await request<ConversationSummary>(`${CONVERSATIONS_PATH}/${conversationId}`);
    } catch {
      return null;
    }
  }, []);

  const updateConversation = useCallback(
    async (
      conversationId: string,
      body: { title?: string | null; messages?: unknown[] },
    ): Promise<ConversationSummary> => {
      return request<ConversationSummary>(`${CONVERSATIONS_PATH}/${conversationId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
    },
    [],
  );

  /**
   * Serialized save queue prevents SSE-triggered concurrent PATCHes from
   * racing on the same conversation row.
   */
  const saveConversation = useCallback(
    async (
      conversationId: string,
      title: string | null,
      messages: unknown[],
      requestToken: number,
    ): Promise<void> => {
      const myToken = ++requestIdRef.current;
      if (myToken !== requestToken) {
        return;
      }
      await updateConversation(conversationId, { title, messages });
    },
    [updateConversation],
  );

  const deleteConversation = useCallback(async (conversationId: string): Promise<void> => {
    await request<void>(`${CONVERSATIONS_PATH}/${conversationId}`, {
      method: "DELETE",
    });
  }, []);

  useEffect(
    () => () => {
      requestIdRef.current += 1;
    },
    [],
  );

  // Every callback above is stable, so memoise the bag: consumers put this
  // object in effect dependency arrays, and a fresh object per render re-runs
  // those effects on every paint (it caused an endless GET /conversations loop).
  return useMemo(
    () => ({
      listConversations,
      createConversation,
      getConversation,
      updateConversation,
      saveConversation,
      deleteConversation,
    }),
    [
      listConversations,
      createConversation,
      getConversation,
      updateConversation,
      saveConversation,
      deleteConversation,
    ],
  );
}