"use client";

import { useCallback, useEffect, useRef } from "react";

import {
  addControllerToScopeSet,
  clearMapAndAbortAllControllers,
  removeControllerFromScopeSet,
  abortAllControllersInScope,
} from "@/lib/abort-controller-scope";
import { authenticatedFetch } from "@/lib/authenticated-fetch";

import type { ChatCompletionMessagePayload } from "./use-chat-completion";

const DEFAULT_API_BASE_URL = "http://localhost:8081";

const PRIOR_MESSAGES_FORM_FIELD = "messages_json";

export interface VoiceCompletionSuccessPayload {
  transcript: string;
  content: string;
}

interface VoiceHandlers {
  onSuccess: (payload: VoiceCompletionSuccessPayload) => void;
  onComplete: () => void;
  onError: (message: string) => void;
  onAbort?: () => void;
}

function messageFromErrorResponseBody(bodyText: string): string {
  const trimmed = bodyText.trim();
  if (trimmed.length === 0) {
    return "";
  }
  try {
    const parsed = JSON.parse(trimmed) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      return parsed.detail;
    }
  } catch {
    return trimmed;
  }
  return trimmed;
}

function getApiBaseUrl(explicit?: string): string {
  const fromEnv = process.env.NEXT_PUBLIC_API_BASE_URL;
  const base = explicit ?? (typeof fromEnv === "string" && fromEnv.length > 0 ? fromEnv : DEFAULT_API_BASE_URL);
  return base.replace(/\/$/, "");
}

export function playAudioFromBase64(audioBase64: string, mimeType: string): HTMLAudioElement {
  const binary = atob(audioBase64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  const blob = new Blob([bytes], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const audioElement = new Audio(url);
  audioElement.addEventListener("ended", () => URL.revokeObjectURL(url));
  audioElement.addEventListener("error", () => URL.revokeObjectURL(url));
  void audioElement.play();
  return audioElement;
}

/** Parallel /voice requests per conversation; closing a tab calls `abortVoiceConversation` for that scope. */
export function useVoiceChat(apiBaseUrl?: string) {
  const abortControllersByScopeRef = useRef<Map<string, Set<AbortController>>>(new Map());

  const abortVoiceConversation = useCallback((conversationScopeId: string) => {
    abortAllControllersInScope(abortControllersByScopeRef.current, conversationScopeId);
  }, []);

  const sendVoiceMessage = useCallback(
    async (
      conversationScopeId: string,
      audioBlob: Blob,
      priorMessages: ChatCompletionMessagePayload[],
      handlers: VoiceHandlers,
    ): Promise<void> => {
      const byScope = abortControllersByScopeRef.current;
      const controller = new AbortController();
      addControllerToScopeSet(byScope, conversationScopeId, controller);

      const baseUrl = getApiBaseUrl(apiBaseUrl);
      const formData = new FormData();
      formData.append("audio", audioBlob, "recording.webm");
      formData.append(PRIOR_MESSAGES_FORM_FIELD, JSON.stringify(priorMessages));
      formData.append("client_conversation_id", conversationScopeId);

      try {
        const response = await authenticatedFetch(`${baseUrl}/voice`, {
          method: "POST",
          body: formData,
          signal: controller.signal,
          timeout: 0, // Long-running agent turn, same as /chat.
        });

        if (!response.ok) {
          const bodyText = await response.text();
          const detail = messageFromErrorResponseBody(bodyText);
          handlers.onError(detail.length > 0 ? detail : response.statusText);
          return;
        }

        const raw = (await response.json()) as Record<string, unknown>;
        if (
          typeof raw.transcript !== "string" ||
          typeof raw.content !== "string" ||
          typeof raw.audio_base64 !== "string" ||
          typeof raw.audio_mime_type !== "string"
        ) {
          handlers.onError("语音接口返回无效响应");
          return;
        }

        playAudioFromBase64(raw.audio_base64, raw.audio_mime_type);

        handlers.onSuccess({ transcript: raw.transcript, content: raw.content });
        handlers.onComplete();
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
    [apiBaseUrl],
  );

  useEffect(
    () => () => {
      clearMapAndAbortAllControllers(abortControllersByScopeRef.current);
    },
    [],
  );

  return { sendVoiceMessage, abortVoiceConversation };
}
