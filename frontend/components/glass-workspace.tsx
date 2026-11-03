"use client";

import Image from "next/image";
import Link from "next/link";
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { AssistantMessage, StreamingAssistantMessage } from "./assistant-message";
import { PendingActionCard } from "./pending-action-card";
import { ChatComposer } from "./chat-composer";
import { ConversationSidebar, type ConversationSidebarItem } from "./conversation-sidebar";
import { WorkspaceShell } from "./workspace-shell";
import { useAuth } from "@/hooks/use-auth";
import { useCalendarIntegration } from "@/hooks/use-calendar-integration";
import { useChatCompletion, type ChatCompletionMessagePayload } from "@/hooks/use-chat-completion";
import {
  useConversations,
  type ConversationSummary,
} from "@/hooks/use-conversations";
import { useNotificationUnreadCount } from "@/hooks/use-notification-unread-count";
import {
  dedupePendingActions,
  upsertPendingAction,
  type PendingActionRecord,
} from "@/lib/pending-actions";
import { useMail163Login } from "@/hooks/use-mail163-login";
import { useNotionIntegration } from "@/hooks/use-notion-integration";
import { useVoiceChat } from "@/hooks/use-voice-chat";
import {
  CHAT_TABS_LOCAL_STORAGE_KEY,
  clearConversationNeedsRefresh,
  clearPersistedChatWorkspaceTabs,
  conversationNeedsRefresh,
  markConversationNeedsRefresh,
} from "@/lib/chat-workspace-storage";

/**
 * 首页默认 Tab 的 id。必须是"每次加载都不同"的真 UUID：后端按
 * `client_conversation_id` 做 upsert，若沿用固定哨兵值 "conv-initial"，
 * 第二个新会话会命中上一次留下的同一行，把自己的消息写进旧会话里
 * （旧标题保留、旧内容被覆盖），也就是"对话莫名改名 / 内容串台"的根源。
 */
const INITIAL_CONVERSATION_ID = createConversationId();

/** 旧版本存进 localStorage 的哨兵 Tab id，读取时需要迁移成真 UUID。 */
const LEGACY_SENTINEL_CONVERSATION_ID = "conv-initial";

const PERSISTED_CHAT_TABS_SCHEMA_VERSION = 1 as const;

const CHAT_TABLIST_LABEL = "对话";

const WORKSPACE_MORE_MENU_DOM_ID = "glass-workspace-more-menu";

const STARTER_PROMPT_GMAIL_SUMMARY =
  "使用已连接的 163 邮箱，列出今天可能需要回复的邮件，并针对每封邮件建议一句简短回复。";

const STARTER_PROMPT_CALENDAR_SNAPSHOT =
  "接下来七天我的本地日历有什么安排？请标出会议之间的冲突或时间紧张点。";

const STARTER_PROMPT_WORKSPACE_CAPABILITIES =
  "根据我保存的偏好、长期记忆和已连接工具，规划我接下来最重要的三件事。";

const STARTER_PROMPT_MEETING_EXTRACT =
  "从这条邀请中提取会议详情，创建提醒，并把后续任务加入我的计划。";

const STARTER_PROMPT_LIFE_CONTEXT =
  "查看今天的天气，并找一个适合短时专注的好去处。";

const WORKSPACE_MENU_LINK_CLASS =
  "flex w-full items-center gap-2 px-3 py-2.5 text-left text-[13px] font-medium text-white/90 transition hover:bg-white/10";

const WORKSPACE_MENU_LOGOUT_CLASS =
  "flex w-full items-center px-3 py-2.5 text-left text-[13px] font-medium text-rose-200 transition hover:bg-rose-500/15";

/** Match `--chat-tab-enter-duration` / `--chat-panel-enter-duration` in app/globals.css */
const CHAT_TAB_ENTER_DURATION_MS = 1200;

const CHAT_ENTER_ANIMATION_BUFFER_MS = 120;

/**
 * Clears enter-animation state after tab grow + panel slide finish (slightly after CSS duration).
 */
const CHAT_ENTER_ANIMATION_CLEAR_MS = CHAT_TAB_ENTER_DURATION_MS + CHAT_ENTER_ANIMATION_BUFFER_MS;

/**
 * How long to keep asking the backend for a transcript that is still being
 * generated after the client dropped the stream. The turn keeps running
 * server-side, so the full reply lands within a few seconds normally.
 */
const CONVERSATION_SYNC_POLL_MS = 1500;
const CONVERSATION_SYNC_DEADLINE_MS = 25000;

function decrementInFlightCount(
  previous: Record<string, number>,
  conversationId: string,
): Record<string, number> {
  const next = { ...previous };
  const nextCount = (next[conversationId] ?? 0) - 1;
  if (nextCount <= 0) {
    delete next[conversationId];
  } else {
    next[conversationId] = nextCount;
  }
  return next;
}

function removeKey<T>(previous: Record<string, T>, key: string): Record<string, T> {
  const next = { ...previous };
  delete next[key];
  return next;
}

/** Fresh local conversation id. Every new tab must get one of these: the backend
 *  upserts by `client_conversation_id`, so a shared sentinel id would make two
 *  distinct conversations fight over the same row. */
function createConversationId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `conv-${Date.now()}`;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

/** Next display name `对话 N`, derived from the highest suffix in use so closing
 *  a middle tab never makes the next tab reuse its name. */
function nextConversationTitle(tabs: readonly ConversationTab[]): string {
  let maxIndex = 0;
  for (const tab of tabs) {
    const match = /^对话\s*(\d+)$/.exec(tab.title.trim());
    if (match) {
      maxIndex = Math.max(maxIndex, Number(match[1]));
    }
  }
  return `对话 ${maxIndex + 1}`;
}

function buildChatMessagesFromPayload(messages: unknown[]): ChatMessage[] {
  if (!Array.isArray(messages)) {
    return [];
  }
  return messages
    .map((raw, index): ChatMessage | null => {
      const message = raw as Record<string, unknown>;
      const role = message.role;
      const text = message.content;
      if (role !== "user" && role !== "assistant") {
        return null;
      }
      if (typeof text !== "string" || text.length === 0) {
        return null;
      }
      const timestamp = typeof message.timestamp === "string" ? message.timestamp : undefined;
      return {
        id: typeof message.id === "string" ? message.id : `msg-${index}-${Date.now()}`,
        role,
        text,
        createdAt: timestamp,
      };
    })
    .filter((message): message is ChatMessage => message !== null);
}

/** Backend rows use snake_case; the card component wants camelCase. */
function buildPendingActionsFromPayload(actions: unknown): PendingActionRecord[] {
  if (!Array.isArray(actions)) {
    return [];
  }
  const records: PendingActionRecord[] = [];
  for (const raw of actions) {
    const row = raw as Record<string, unknown>;
    const id = typeof row.id === "string" ? row.id : null;
    const actionType = row.action_type ?? row.actionType;
    const status = row.status;
    if (!id || typeof actionType !== "string" || typeof status !== "string") {
      continue;
    }
    // 已处理完的草案不需要重新弹卡片；只有 pending 才等待用户操作。
    if (status !== "pending") {
      continue;
    }
    records.push({
      id,
      actionType: actionType as PendingActionRecord["actionType"],
      status: status as PendingActionRecord["status"],
      title: typeof row.title === "string" ? row.title : "",
      summary: typeof row.summary === "string" ? row.summary : null,
      payload: (row.payload ?? {}) as Record<string, unknown>,
      createdAt: typeof row.created_at === "string" ? row.created_at : null,
      expiresAt: typeof row.expires_at === "string" ? row.expires_at : null,
    });
  }
  return dedupePendingActions(records);
}

/**
 * 把未确认的草案挂回它所属的那条 assistant 消息。
 *
 * 草案存在 `pending_actions` 表，聊天记录存在 `conversations.messages`，两者
 * 分开存。切页/刷新只重拉后者，卡片因此凭空消失——这里把接口带回的草案补上。
 * 挂到最后一条 assistant 消息下面，因为草案是那一轮生成的。
 */
function attachPendingActions(messages: ChatMessage[], actions: unknown): ChatMessage[] {
  const drafts = buildPendingActionsFromPayload(actions);
  if (drafts.length === 0) {
    return messages;
  }
  let target = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === "assistant") {
      target = index;
      break;
    }
  }
  if (target < 0) {
    return messages;
  }
  const existing = messages[target].pendingActions ?? [];
  const merged = [...existing];
  for (const draft of drafts) {
    const index = merged.findIndex((item) => item.id === draft.id);
    if (index >= 0) {
      merged[index] = draft;
    } else {
      merged.push(draft);
    }
  }
  const next = [...messages];
  next[target] = { ...next[target], pendingActions: merged };
  return next;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  /** ISO time when this message was created (used by backend to isolate latest request vs history). */
  createdAt?: string;
  /** Assistant replies from voice use plain text (no markdown parsing). */
  isPlainText?: boolean;
  /** HITL confirmation drafts attached to this assistant message (confirm cards). */
  pendingActions?: PendingActionRecord[];
}

const TAB_ICON_ACCENT_CLASSES = [
  "bg-gradient-to-br from-pink-400 to-rose-500",
  "bg-gradient-to-br from-sky-400 to-blue-600",
  "bg-gradient-to-br from-violet-400 to-purple-600",
] as const;

interface ConversationTab {
  id: string;
  title: string;
}

interface PersistedChatTabsPayload {
  readonly version: typeof PERSISTED_CHAT_TABS_SCHEMA_VERSION;
  readonly conversations: readonly ConversationTab[];
  readonly activeConversationId: string;
}

function parsePersistedChatTabs(raw: string): PersistedChatTabsPayload | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) {
      return null;
    }
    const record = parsed as Record<string, unknown>;
    if (record.version !== PERSISTED_CHAT_TABS_SCHEMA_VERSION) {
      return null;
    }
    if (typeof record.activeConversationId !== "string" || record.activeConversationId.length === 0) {
      return null;
    }
    const rawList = record.conversations;
    if (!Array.isArray(rawList) || rawList.length === 0) {
      return null;
    }
    const conversations: ConversationTab[] = [];
    for (const entry of rawList) {
      if (typeof entry !== "object" || entry === null) {
        return null;
      }
      const row = entry as Record<string, unknown>;
      if (typeof row.id !== "string" || row.id.length === 0 || typeof row.title !== "string") {
        return null;
      }
      conversations.push({ id: row.id, title: row.title });
    }
    const activeInList = conversations.some((c) => c.id === record.activeConversationId);
    const activeConversationId = activeInList ? record.activeConversationId : conversations[0].id;
    return {
      version: PERSISTED_CHAT_TABS_SCHEMA_VERSION,
      conversations,
      activeConversationId,
    };
  } catch {
    return null;
  }
}

function readPersistedChatTabsFromStorage(): PersistedChatTabsPayload | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(CHAT_TABS_LOCAL_STORAGE_KEY);
    if (!raw) {
      return null;
    }
    return parsePersistedChatTabs(raw);
  } catch {
    return null;
  }
}

function writePersistedChatTabsToStorage(payload: PersistedChatTabsPayload): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(CHAT_TABS_LOCAL_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Private mode or quota — workspace still works for the session
  }
}

function IconPlus({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function IconDotsHorizontal({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor" aria-hidden>
      <circle cx="6" cy="12" r="2" />
      <circle cx="12" cy="12" r="2" />
      <circle cx="18" cy="12" r="2" />
    </svg>
  );
}

function IconCloseSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M18 6L6 18M6 6l12 12" />
    </svg>
  );
}

function IconMail({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
      />
    </svg>
  );
}

function IconCalendarSimple({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
      />
    </svg>
  );
}

function IconLayers({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5"
      />
    </svg>
  );
}

function IconCheckSquare({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
      />
    </svg>
  );
}

function IconBellNav({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
      />
    </svg>
  );
}

const TOP_BAR_ICON_LINK_CLASS =
  "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/25 bg-white/14 text-white/90 shadow-sm transition hover:border-white/35 hover:bg-white/22";

export function GlassWorkspace() {
  const queryClient = useQueryClient();
  const { user, logout } = useAuth();
  const conversationsApi = useConversations();
  const {
    requestAssistantReply,
    abortConversation: abortChatForConversation,
    cancelConversation: cancelChatForConversation,
  } = useChatCompletion();
  const { sendVoiceMessage, abortVoiceConversation: abortVoiceForConversation } = useVoiceChat();
  const { unreadCount: topBarNotificationCount } = useNotificationUnreadCount();
  const { status: mail163Status, connectMail163, disconnectMail163 } = useMail163Login();
  const { status: calendarStatus, connectCalendar, disconnectCalendar } = useCalendarIntegration();
  const { status: notionStatus, connectNotion, disconnectNotion } = useNotionIntegration();

  /** After a chat/voice turn the backend may create or mutate tasks, reminders and
   *  calendar events. Invalidate every data source the sidebars/cards render so the
   *  left task overview and "下一项日程" card refresh without a manual reload. */
  const invalidateDataAfterChat = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["tasks"] });
    void queryClient.invalidateQueries({ queryKey: ["tasks", "scan-plan"] });
    void queryClient.invalidateQueries({ queryKey: ["calendar", "events"] });
    void queryClient.invalidateQueries({ queryKey: ["life-pilot", "overview"] });
  }, [queryClient]);

  /** Count of in-flight requests per conversation (text and/or voice can run in parallel). */
  const [repliesInFlight, setRepliesInFlight] = useState<Record<string, number>>({});
  /** Live status message per conversation, shown while a reply is in-flight. */
  const [statusByConversationId, setStatusByConversationId] = useState<Record<string, string>>({});
  /** Incremental assistant text streamed via token events, keyed by conversation id. */
  const [streamingByConversationId, setStreamingByConversationId] = useState<Record<string, string>>({});

  const [conversations, setConversations] = useState<ConversationTab[]>(() => [
    { id: INITIAL_CONVERSATION_ID, title: "对话 1" },
  ]);
  const [activeConversationId, setActiveConversationId] = useState<string>(INITIAL_CONVERSATION_ID);
  const [draftsByConversationId, setDraftsByConversationId] = useState<Record<string, string>>(() => ({
    [INITIAL_CONVERSATION_ID]: "",
  }));

  const [messagesByConversationId, setMessagesByConversationId] = useState<Record<string, ChatMessage[]>>(() => ({}));

  /** After first client effect: restored from localStorage or default; avoids overwriting storage before read. */
  const [hasHydratedChatTabs, setHasHydratedChatTabs] = useState(false);

  /** Conversation ids already persisted to the backend (deduped against the restore). */
  const [persistedConversationIds, setPersistedConversationIds] = useState<Set<string>>(() => new Set());

  /** Server-backed history rows shown in the left sidebar. Deliberately separate
   *  from the open tabs: closing a tab must NOT remove the conversation here, so
   *  it stays reopenable from the history list until explicitly deleted. */
  const [historyConversations, setHistoryConversations] = useState<ConversationSidebarItem[]>([]);

  /** Chat-thread sidebar visibility (desktop default expanded). */
  const [isChatSidebarExpanded, setIsChatSidebarExpanded] = useState(true);

  /** HITL drafts streamed in during the current turn, attached to the next assistant message. */
  const pendingActionsForTurnRef = useRef<PendingActionRecord[]>([]);

  /** Live-mirrors messagesByConversationId so the debounced saver never reads stale closures. */
  const messagesByConversationIdRef = useRef<Record<string, ChatMessage[]>>({});
  /** Live-mirrors streamingByConversationId so `onSuccess` can tell whether the
   *  turn was already rendered token-by-token (and must not be replayed). */
  const streamingByConversationIdRef = useRef<Record<string, string>>({});
  /** Live-mirrors persistedConversationIds for the same reason. */
  const persistedConversationIdsRef = useRef<Set<string>>(new Set());
  /** Conversation titles keyed by id for PATCH title updates. */
  const conversationsTitleRef = useRef<Record<string, string>>({});
  /** Pending debounced saves, one timer per conversation id. */
  const persistDebounceTimeoutRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  /** Read by the one-shot history hydration effect without re-running it per tab switch. */
  const activeConversationIdRef = useRef<string>(INITIAL_CONVERSATION_ID);
  /** Live-mirrors `repliesInFlight` so the unmount flush can skip in-flight turns. */
  const repliesInFlightRef = useRef<Record<string, number>>({});
  /** Cancels a server-side transcript poll when the workspace unmounts. */
  const conversationSyncCancelledRef = useRef(false);

  useEffect(() => {
    messagesByConversationIdRef.current = messagesByConversationId;
  }, [messagesByConversationId]);

  useEffect(() => {
    streamingByConversationIdRef.current = streamingByConversationId;
  }, [streamingByConversationId]);

  useEffect(() => {
    repliesInFlightRef.current = repliesInFlight;
  }, [repliesInFlight]);

  useEffect(() => {
    activeConversationIdRef.current = activeConversationId;
  }, [activeConversationId]);

  /**
   * 打断这一轮：先让后端取消 LangGraph，再断开本地流。
   *
   * 顺序不能反。`abort()` 只是关连接，后端无法区分"用户点停止"和"网络抖动"，
   * 会把新一轮的 token 白烧完；先发 cancel 才是明确意图。
   */
  const interruptReply = useCallback(
    (conversationId: string) => {
      if ((repliesInFlightRef.current[conversationId] ?? 0) <= 0) {
        return;
      }
      void cancelChatForConversation(conversationId);
      abortChatForConversation(conversationId);
      abortVoiceForConversation(conversationId);
    },
    [abortChatForConversation, abortVoiceForConversation, cancelChatForConversation],
  );

  const cancelScheduledSave = useCallback((conversationId: string) => {
    const timer = persistDebounceTimeoutRef.current.get(conversationId);
    if (timer !== undefined) {
      clearTimeout(timer);
      persistDebounceTimeoutRef.current.delete(conversationId);
    }
  }, []);

  useEffect(() => {
    persistedConversationIdsRef.current = persistedConversationIds;
  }, [persistedConversationIds]);

  useEffect(() => {
    conversationsTitleRef.current = Object.fromEntries(conversations.map((conversation) => [conversation.id, conversation.title]));
    void 0;
  }, [conversations]);

  const [isMoreMenuOpen, setIsMoreMenuOpen] = useState(false);
  const [isLogoutInFlight, setIsLogoutInFlight] = useState(false);
  const moreMenuContainerRef = useRef<HTMLDivElement | null>(null);

  /** IDs of assistant messages that should reveal with the typewriter effect. */
  const [animatingMessageIds, setAnimatingMessageIds] = useState<Set<string>>(() => new Set());

  const handleTypingComplete = useCallback((messageId: string) => {
    setAnimatingMessageIds((previous) => {
      const next = new Set(previous);
      next.delete(messageId);
      return next;
    });
  }, []);

  /** Set when a conversation is created via "New"; drives slide-in on tab + panel */
  const [enteringConversationId, setEnteringConversationId] = useState<string | null>(null);

  const enterAnimationClearTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const chatTabListRef = useRef<HTMLDivElement | null>(null);

  const activeDraft = draftsByConversationId[activeConversationId] ?? "";
  const messagesForActiveConversation = messagesByConversationId[activeConversationId] ?? [];
  const hasConversationMessages = messagesForActiveConversation.length > 0;
  /** 当前会话是否有一轮正在生成 — 决定发送键显示「发送」还是「停止」。 */
  const isActiveReplyStreaming = (repliesInFlight[activeConversationId] ?? 0) > 0;

  const messagesScrollRef = useRef<HTMLDivElement | null>(null);

  /** Latest `saveConversationNow`, so the unmount handler can flush instead of
   *  dropping the pending debounce write (that was losing the last reply when
   *  the user switched pages). */
  const saveConversationNowRef = useRef<(conversationId: string) => void>(() => {});

  useEffect(() => {
    return () => {
      conversationSyncCancelledRef.current = true;
      if (enterAnimationClearTimeoutRef.current !== null) {
        clearTimeout(enterAnimationClearTimeoutRef.current);
      }
      // 卸载时必须**先冲刷**再清定时器：切页会卸载整个工作区，直接丢弃
      // 待写队列就等于丢掉刚生成的那条回复。
      //
      // 但"仍在生成中"的会话绝不能冲刷：本地只有半截回复，PATCH 上去会把
      // 服务端正在生成的完整对话截断。这类会话打上待刷新标记，回来时从
      // 服务端重新拉取（后端已把断线的那一轮完整落库）。
      for (const conversationId of persistDebounceTimeoutRef.current.keys()) {
        const inFlight = saveConversationNowRef.current;
        if ((repliesInFlightRef.current[conversationId] ?? 0) > 0) {
          markConversationNeedsRefresh(conversationId);
          continue;
        }
        inFlight(conversationId);
      }
      for (const timer of persistDebounceTimeoutRef.current.values()) {
        clearTimeout(timer);
      }
      persistDebounceTimeoutRef.current.clear();
    };
  }, []);

  useEffect(() => {
    const persisted = readPersistedChatTabsFromStorage();
    if (persisted !== null) {
      // 刷新后最多只恢复"最后激活的那一个" Tab：把其它会话留在历史列表，
      // 不把整排历史会话都铺满 Tab 条。
      const lastActive = persisted.conversations.find(
        (conversation) => conversation.id === persisted.activeConversationId,
      );
      const restored = [
        lastActive ?? persisted.conversations[persisted.conversations.length - 1],
      ].map((conversation) =>
        // 旧 localStorage 里可能存着非 UUID 的哨兵 id（"conv-initial"）：迁移成
        // 真 UUID，避免它再次被当成 client_conversation_id 复用旧行。
        isUuid(conversation.id) ? conversation : { ...conversation, id: createConversationId() },
      );
      setConversations(restored);
      setActiveConversationId(restored[0].id);
      setDraftsByConversationId(() => {
        const next: Record<string, string> = {};
        for (const conversation of restored) {
          next[conversation.id] = "";
        }
        return next;
      });
      setMessagesByConversationId(() => {
        const next: Record<string, ChatMessage[]> = {};
        for (const conversation of restored) {
          next[conversation.id] = [];
        }
        return next;
      });
    }

    const starterQuery = new URLSearchParams(window.location.search).get("q");
    if (starterQuery) {
      const targetId = persisted?.activeConversationId ?? INITIAL_CONVERSATION_ID;
      setDraftsByConversationId((previous) => ({
        ...previous,
        [targetId]: starterQuery,
      }));
      window.history.replaceState(null, "", window.location.pathname);
    }

    setHasHydratedChatTabs(true);
  }, []);

  useEffect(() => {
    if (!hasHydratedChatTabs) {
      return;
    }
    // Only remember tabs that actually hold a turn: an untouched "对话 N" tab
    // should not survive a refresh, and an empty conversation is never created
    // server-side either.
    const restorable = conversations.filter(
      (conversation) => (messagesByConversationId[conversation.id] ?? []).length > 0,
    );
    if (restorable.length === 0) {
      clearPersistedChatWorkspaceTabs();
      return;
    }
    const restorableActive = restorable.some(
      (conversation) => conversation.id === activeConversationId,
    )
      ? activeConversationId
      : restorable[restorable.length - 1].id;
    writePersistedChatTabsToStorage({
      version: PERSISTED_CHAT_TABS_SCHEMA_VERSION,
      conversations: restorable,
      activeConversationId: restorableActive,
    });
  }, [activeConversationId, conversations, hasHydratedChatTabs, messagesByConversationId]);

  // Hydrate the sidebar history from the server once. The tab strip keeps only
  // the single tab restored from localStorage — listing every stored
  // conversation there would reopen the whole history on each reload.
  useEffect(() => {
    if (!hasHydratedChatTabs) {
      return;
    }
    let cancelled = false;
    void conversationsApi
      .listConversations()
      .then((remote: ConversationSummary[]) => {
        if (cancelled || remote.length === 0) {
          return;
        }
        const remoteItems: ConversationSidebarItem[] = remote.map((conversation) => ({
          id: conversation.id,
          title: conversation.title?.trim() || "无标题对话",
          persisted: true,
        }));
        const titleById = new Map(remoteItems.map((item) => [item.id, item.title]));
        setPersistedConversationIds(new Set(titleById.keys()));
        setHistoryConversations(remoteItems);

        // 恢复出来的 Tab 若在服务端已经不存在（被删掉 / 从未落库），换一条全新的
        // 空会话，并且必须给它一个新 id：沿用同一个 id 会让下一段对话继续命中
        // 旧行（服务端按 client_conversation_id upsert），表现就是"自己改回对话1、
        // 内容串进旧会话"。
        const restoredTabId = activeConversationIdRef.current;
        const restoredTitle = titleById.get(restoredTabId);
        if (restoredTitle === undefined) {
          const freshId = createConversationId();
          setConversations([{ id: freshId, title: "对话 1" }]);
          setDraftsByConversationId({ [freshId]: "" });
          setMessagesByConversationId({});
          setActiveConversationId(freshId);
        } else {
          setConversations([{ id: restoredTabId, title: restoredTitle }]);
        }

        const persistedActiveId = activeConversationIdRef.current;
        const persistedActive = titleById.has(persistedActiveId);
        if (persistedActive) {
          void conversationsApi
            .getConversation(persistedActiveId)
            .then((conversation) => {
              if (!conversation || cancelled) {
                return;
              }
              const restored = attachPendingActions(
                buildChatMessagesFromPayload(conversation.messages),
                conversation.pending_actions,
              );
              setMessagesByConversationId((previous) => {
                // Never clobber a thread that already holds local turns (e.g. a
                // reply that landed before the debounced save finished).
                if ((previous[conversation.id] ?? []).length > 0) {
                  return previous;
                }
                return { ...previous, [conversation.id]: restored };
              });
            })
            .catch(() => {
              // Corrupt/partial message JSON: keep the empty thread.
            });
        }
      })
      .catch(() => {
        // Backend unreachable — keep the local default conversation.
      });
    return () => {
      cancelled = true;
    };
  }, [hasHydratedChatTabs, conversationsApi]);

  // 渲染后滚到底部：用 useLayoutEffect 在浏览器绘制前完成，避免"先闪一下
  // 顶部再跳到底部"；smooth 在消息刚插入时经常被后续布局打断，改用即时滚动。
  useLayoutEffect(() => {
    const element = messagesScrollRef.current;
    if (!element || !hasConversationMessages) {
      return;
    }
    element.scrollTop = element.scrollHeight;
  }, [activeConversationId, hasConversationMessages, messagesForActiveConversation]);

  const isAnyMessageAnimating = animatingMessageIds.size > 0;

  useEffect(() => {
    const element = messagesScrollRef.current;
    if (!element || !isAnyMessageAnimating) {
      return;
    }

    let frameId: number;

    const scrollTick = () => {
      element.scrollTop = element.scrollHeight;
      frameId = requestAnimationFrame(scrollTick);
    };

    frameId = requestAnimationFrame(scrollTick);

    return () => cancelAnimationFrame(frameId);
  }, [isAnyMessageAnimating]);

  useLayoutEffect(() => {
    if (!enteringConversationId) {
      return;
    }

    const list = chatTabListRef.current;
    if (!list) {
      return;
    }

    let isCancelled = false;

    const easeInOutCubic = (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

    const runScroll = (): void => {
      const startScroll = list.scrollLeft;
      const startTime = performance.now();

      const tick = (now: number): void => {
        if (isCancelled) {
          return;
        }
        const elapsed = now - startTime;
        const t = Math.min(1, elapsed / CHAT_TAB_ENTER_DURATION_MS);
        const eased = easeInOutCubic(t);
        const targetScroll = Math.max(0, list.scrollWidth - list.clientWidth);
        list.scrollLeft = startScroll + (targetScroll - startScroll) * eased;
        if (t < 1) {
          requestAnimationFrame(tick);
        }
      };

      requestAnimationFrame(tick);
    };

    const startId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (!isCancelled) {
          runScroll();
        }
      });
    });

    return () => {
      isCancelled = true;
      cancelAnimationFrame(startId);
    };
  }, [enteringConversationId]);

  const setActiveDraft = useCallback(
    (value: string) => {
      setDraftsByConversationId((previous) => ({
        ...previous,
        [activeConversationId]: value,
      }));
    },
    [activeConversationId],
  );

  const applyStarterPromptToComposer = useCallback(
    (prompt: string) => {
      setActiveDraft(prompt);
    },
    [setActiveDraft],
  );

  /**
   * Ensure a conversation id has a tab in the strip, then activate it. Opening
   * a conversation from the left history list must surface it in the tabs so
   * the active tab + the active id stay in sync.
   */
  const openConversationTab = useCallback(
    (conversationId: string) => {
      const alreadyOpen = conversations.some((conversation) => conversation.id === conversationId);
      if (!alreadyOpen) {
        const historyTitle = historyConversations.find((item) => item.id === conversationId)?.title;
        const next: ConversationTab = {
          id: conversationId,
          title: historyTitle?.trim() || "对话",
        };
        setConversations((previous) => [...previous, next]);
        if (enterAnimationClearTimeoutRef.current !== null) {
          clearTimeout(enterAnimationClearTimeoutRef.current);
        }
        setEnteringConversationId(conversationId);
        enterAnimationClearTimeoutRef.current = setTimeout(() => {
          setEnteringConversationId(null);
          enterAnimationClearTimeoutRef.current = null;
        }, CHAT_ENTER_ANIMATION_CLEAR_MS);
      }
      setActiveConversationId(conversationId);
    },
    [conversations, historyConversations],
  );

  /**
   * Select a conversation. When it is server-persisted but its messages are
   * not already cached locally, lazily fetch the transcript so the thread is
   * recoverable from the sidebar even after a reload.
   */
  const handleSelectConversation = useCallback(
    (conversationId: string) => {
      openConversationTab(conversationId);
      const messages = messagesByConversationIdRef.current[conversationId];
      if (messages && messages.length > 0) {
        return;
      }
      if (!persistedConversationIdsRef.current.has(conversationId)) {
        return;
      }
      void conversationsApi
        .getConversation(conversationId)
        .then((conversation) => {
          if (!conversation) {
            return;
          }
          const restored = attachPendingActions(
            buildChatMessagesFromPayload(conversation.messages),
            conversation.pending_actions,
          );
          if (restored.length === 0) {
            return;
          }
          setMessagesByConversationId((previous) => {
            // A turn may have finished while the fetch was in flight; the local
            // transcript is then newer than what we just read, so keep it.
            if ((previous[conversationId] ?? []).length > 0) {
              return previous;
            }
            return { ...previous, [conversationId]: restored };
          });
        })
        .catch(() => {
          // Backend unavailable — keep the empty thread.
        });
    },
    [conversationsApi, openConversationTab],
  );

  useEffect(() => {
    if (!isMoreMenuOpen) {
      return;
    }
    const handlePointerDown = (event: MouseEvent) => {
      const node = moreMenuContainerRef.current;
      if (node && event.target instanceof Node && !node.contains(event.target)) {
        setIsMoreMenuOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsMoreMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isMoreMenuOpen]);

  const handleWorkspaceLogout = useCallback(async () => {
    if (isLogoutInFlight) {
      return;
    }
    setIsLogoutInFlight(true);
    setIsMoreMenuOpen(false);
    try {
      queryClient.clear();
      clearPersistedChatWorkspaceTabs();
      await logout();
    } finally {
      setIsLogoutInFlight(false);
    }
  }, [isLogoutInFlight, logout, queryClient]);

  /**
   * Swap a local-only conversation id for its server-persisted id. Keeps the
   * tab stable while the backend row is created once on first save.
   */
  const swapConversationId = useCallback(
    (oldId: string, newId: string) => {
      // The row now exists under newId; a stale timer would try to recreate oldId.
      cancelScheduledSave(oldId);
      setConversations((previous) =>
        previous.map((conversation) =>
          conversation.id === oldId
            ? { id: newId, title: conversationsTitleRef.current[oldId] ?? conversation.title }
            : conversation,
        ),
      );
      setMessagesByConversationId((previous) => {
        messagesByConversationIdRef.current = {
          ...messagesByConversationIdRef.current,
          [newId]: messagesByConversationIdRef.current[oldId] ?? previous[oldId] ?? [],
        };
        delete messagesByConversationIdRef.current[oldId];
        return messagesByConversationIdRef.current;
      });
      setDraftsByConversationId((previous) => {
        const next = { ...previous };
        next[newId] = next[oldId] ?? "";
        delete next[oldId];
        return next;
      });
      setRepliesInFlight((previous) => {
        const next = { ...previous };
        if (next[oldId] !== undefined) {
          next[newId] = next[oldId];
          delete next[oldId];
        }
        return next;
      });
      setStatusByConversationId((previous) => {
        const next = { ...previous };
        if (next[oldId] !== undefined) {
          next[newId] = next[oldId];
          delete next[oldId];
        }
        return next;
      });
      setStreamingByConversationId((previous) => {
        const next = { ...previous };
        if (next[oldId] !== undefined) {
          next[newId] = next[oldId];
          delete next[oldId];
        }
        return next;
      });
      if (activeConversationId === oldId) {
        setActiveConversationId(newId);
      }
      setPersistedConversationIds((previous) => {
        const next = new Set(previous);
        next.add(newId);
        return next;
      });
      persistedConversationIdsRef.current = new Set([...persistedConversationIdsRef.current, newId]);
    },
    [activeConversationId, cancelScheduledSave],
  );

  /**
   * Re-sync titles for already-persisted conversations from the backend (e.g.
   * after the server's background LLM refines a placeholder/truncation title).
   * Only touches persisted rows; never overwrites a locally-active tab's title.
   */
  const refreshConversationTitles = useCallback(() => {
    void conversationsApi.listConversations().then((remote) => {
      const titlesById = new Map<string, string | null>(
        remote.map((conversation) => [conversation.id, conversation.title?.trim() || null]),
      );
      setConversations((previous) => {
        let changed = false;
        const next = previous.map((conversation) => {
          if (!persistedConversationIdsRef.current.has(conversation.id)) {
            return conversation;
          }
          const remoteTitle = titlesById.get(conversation.id);
          if (typeof remoteTitle === "string" && remoteTitle !== conversation.title) {
            changed = true;
            return { ...conversation, title: remoteTitle };
          }
          return conversation;
        });
        return changed ? next : previous;
      });
      setHistoryConversations((previous) => {
        let changed = false;
        const next = previous.map((item) => {
          const remoteTitle = titlesById.get(item.id);
          if (typeof remoteTitle === "string" && remoteTitle !== item.title) {
            changed = true;
            return { ...item, title: remoteTitle };
          }
          return item;
        });
        return changed ? next : previous;
      });
    });
  }, [conversationsApi]);

  /**
   * Save a conversation's transcript + auto-derived title to the backend,
   * creating the row on first save. Empty transcripts are never written, so a
   * brand-new tab the user never typed into leaves no row behind.
   */
  const saveConversationNow = useCallback(
    (conversationId: string) => {
      const messages = messagesByConversationIdRef.current[conversationId] ?? [];
      if (messages.length === 0) {
        return;
      }
      const payload = messages
        .filter((message) => message.text.trim().length > 0)
        .map((message) => ({
          role: message.role,
          content: message.text.trim(),
          timestamp: message.createdAt,
        }));
      if (payload.length === 0) {
        return;
      }

      const currentTitle = (conversationsTitleRef.current[conversationId] ?? "").trim();
      const isPlaceholderTitle =
        currentTitle === "" ||
        currentTitle === "无标题对话" ||
        /^对话$/.test(currentTitle) ||
        /^对话\s*\d+$/.test(currentTitle) ||
        !isUuid(conversationId);
      // Pass a placeholder (or empty) title on create so the backend generates a
      // real title; itself never forces a stale "对话 N" onto the row.
      const title = isPlaceholderTitle ? "" : currentTitle;

      const alreadyPersisted = persistedConversationIdsRef.current.has(conversationId);
      const run = alreadyPersisted
        ? conversationsApi.updateConversation(conversationId, { messages: payload }).then((updated) => {
            // If this row was closed but not deleted, keep it in the history list.
            const updatedTitle = updated.title?.trim();
            setHistoryConversations((previous) => {
              if (previous.some((item) => item.id === conversationId)) {
                return previous;
              }
              return [
                { id: conversationId, title: updatedTitle || conversationsTitleRef.current[conversationId]?.trim() || "无标题对话", persisted: true },
                ...previous,
              ];
            });
            return updated;
          })
        : conversationsApi.createConversation(title || null, payload, conversationId).then((created) => {
            if (created.id !== conversationId) {
              swapConversationId(conversationId, created.id);
            }
            const createdTitle = created.title;
            const effectiveId = created.id === conversationId ? conversationId : created.id;
            // 关键：建行成功后必须把 id 记为"已落库"。旧实现只在 id 被替换
            // （swapConversationId）时才登记，于是"新建即可删除"的会话永远
            // 被认为没有服务端行 → 删除时跳过 DELETE → 刷新后又冒出来。
            setPersistedConversationIds((previous) => {
              if (previous.has(effectiveId)) {
                return previous;
              }
              const next = new Set(previous);
              next.add(effectiveId);
              return next;
            });
            persistedConversationIdsRef.current = new Set([...persistedConversationIdsRef.current, effectiveId]);
            if (createdTitle) {
              setConversations((previous) =>
                previous.map((conversation) =>
                  conversation.id === effectiveId ? { ...conversation, title: createdTitle } : conversation,
                ),
              );
            }
            // Newly persisted conversation belongs in the sidebar history even
            // if its tab is closed later.
            setHistoryConversations((previous) => {
              const withoutOld = previous.filter(
                (item) => item.id !== conversationId && item.id !== effectiveId,
              );
              return [
                {
                  id: effectiveId,
                  title:
                    createdTitle?.trim() ||
                    conversationsTitleRef.current[conversationId]?.trim() ||
                    "无标题对话",
                  persisted: true,
                },
                ...withoutOld,
              ];
            });
            // The backend may still be refining the title in a background task;
            // re-sync titles shortly after so the sidebar picks it up.
            setTimeout(() => {
              refreshConversationTitles();
            }, 2500);
            return created;
          });

      run.catch(() => {
        // Backend unavailable — keep working locally and retry next debounce.
      });
    },
    [conversationsApi, refreshConversationTitles, swapConversationId],
  );

  useEffect(() => {
    saveConversationNowRef.current = saveConversationNow;
  }, [saveConversationNow]);

  /** Debounced save. The timer is per conversation: saving one tab must not
   *  cancel another tab's pending write (that silently dropped replies when the
   *  user switched away mid-turn). */
  const persistConversation = useCallback(
    (conversationId: string) => {
      cancelScheduledSave(conversationId);
      const timer = setTimeout(() => {
        persistDebounceTimeoutRef.current.delete(conversationId);
        saveConversationNow(conversationId);
      }, 800);
      persistDebounceTimeoutRef.current.set(conversationId, timer);
    },
    [cancelScheduledSave, saveConversationNow],
  );

  /** Flush a pending save immediately (tab close / unmount), so closing a tab
   *  cannot discard a reply the debounce had not written yet. */
  const flushConversationSave = useCallback(
    (conversationId: string) => {
      const hadTimer = persistDebounceTimeoutRef.current.has(conversationId);
      cancelScheduledSave(conversationId);
      if (!hadTimer) {
        return;
      }
      // 生成中的会话本地只有半截回复，写上去就是截断服务端正在生成的完整对话。
      if ((repliesInFlightRef.current[conversationId] ?? 0) > 0) {
        markConversationNeedsRefresh(conversationId);
        return;
      }
      saveConversationNow(conversationId);
    },
    [cancelScheduledSave, saveConversationNow],
  );

  /**
   * Pull the authoritative transcript from the server after a dropped stream.
   *
   * 客户端断开后后端仍在生成并会把完整对话落库，所以这里轮询到"本地缺失的
   * 那一轮"出现为止：消息条数超过断开时的条数即视为拿到完整结果。
   */
  const syncConversationFromServer = useCallback(
    async (conversationId: string) => {
      const baselineCount = (messagesByConversationIdRef.current[conversationId] ?? []).length;
      const deadline = Date.now() + CONVERSATION_SYNC_DEADLINE_MS;
      while (!conversationSyncCancelledRef.current && Date.now() < deadline) {
        const conversation = await conversationsApi.getConversation(conversationId);
        if (conversation) {
          const restored = attachPendingActions(
            buildChatMessagesFromPayload(conversation.messages),
            conversation.pending_actions,
          );
          if (restored.length > baselineCount) {
            setMessagesByConversationId((previous) => ({
              ...previous,
              [conversationId]: restored,
            }));
            clearConversationNeedsRefresh(conversationId);
            invalidateDataAfterChat();
            return;
          }
        }
        await new Promise((resolve) => setTimeout(resolve, CONVERSATION_SYNC_POLL_MS));
      }
      // 超时也清掉标记：下次进入会话时若服务端更完整，handleSelectConversation
      // 会再拉一次，而不是让标记永久卡住本地数据。
      clearConversationNeedsRefresh(conversationId);
    },
    [conversationsApi, invalidateDataAfterChat],
  );

  // 回到工作区（刷新 / 切页返回）时，补拉断线那一轮被服务端续写完成的对话。
  useEffect(() => {
    if (!hasHydratedChatTabs) {
      return;
    }
    conversationSyncCancelledRef.current = false;
    if (!conversationNeedsRefresh(activeConversationId)) {
      return;
    }
    void syncConversationFromServer(activeConversationId);
  }, [activeConversationId, hasHydratedChatTabs, syncConversationFromServer]);

  const handleSendChatMessage = useCallback(
    (plainText: string) => {
      const trimmedText = plainText.trim();
      if (!trimmedText) {
        return;
      }

      const conversationId = activeConversationId;

      // 打断重入：同一会话永远只跑一轮。上一轮还在生成时，先取消再发新的，
      // 绝不并发（并发会让两条流的 token 交错写进同一个气泡）。
      interruptReply(conversationId);

      const messageId =
        typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `msg-${Date.now()}`;
      const userMessage: ChatMessage = {
        id: messageId,
        role: "user",
        text: trimmedText,
        createdAt: new Date().toISOString(),
      };

      const historyAfterUser: ChatMessage[] = [
        ...(messagesByConversationId[conversationId] ?? []),
        userMessage,
      ];

      setMessagesByConversationId((previous) => ({
        ...previous,
        [conversationId]: [...(previous[conversationId] ?? []), userMessage],
      }));

      setDraftsByConversationId((previous) => ({
        ...previous,
        [conversationId]: "",
      }));

      setStreamingByConversationId((previous) => removeKey(previous, conversationId));
      // Also clear the mirror now: the effect that syncs it runs after render, and
      // a turn that finishes fast could otherwise see the previous turn's tokens.
      streamingByConversationIdRef.current = removeKey(
        streamingByConversationIdRef.current,
        conversationId,
      );

      const apiMessages: ChatCompletionMessagePayload[] = historyAfterUser
        .filter((message) => message.text.trim().length > 0)
        .map((message) => ({
          role: message.role,
          content: message.text.trim(),
          timestamp: message.createdAt,
        }));

      setRepliesInFlight((previous) => ({
        ...previous,
        [conversationId]: (previous[conversationId] ?? 0) + 1,
      }));

      queueMicrotask(() => {
        pendingActionsForTurnRef.current = [];
        void requestAssistantReply(conversationId, apiMessages, {
          onStatus: (statusMessage) => {
            setStatusByConversationId((previous) => ({ ...previous, [conversationId]: statusMessage }));
          },
          onToken: (delta) => {
            // 同步写 ref：`done` 可能与最后几个 token 在同一批 SSE 帧里到达，
            // 只靠 effect 同步会让 onSuccess 误判"没流式输出过"从而重播全文。
            streamingByConversationIdRef.current = {
              ...streamingByConversationIdRef.current,
              [conversationId]:
                (streamingByConversationIdRef.current[conversationId] ?? "") + delta,
            };
            setStreamingByConversationId((previous) => ({
              ...previous,
              [conversationId]: (previous[conversationId] ?? "") + delta,
            }));
          },
          onPendingAction: (action) => {
            // 同一轮里模型可能重复提交（并发 tool_calls / 被打回重写），
            // SSE 重连也可能重放事件。按意图指纹合并，避免弹两张一样的确认卡。
            pendingActionsForTurnRef.current = upsertPendingAction(
              pendingActionsForTurnRef.current,
              action,
            );
          },
          onSuccess: (assistantText) => {
            const assistantMessageId =
              typeof crypto !== "undefined" && "randomUUID" in crypto
                ? crypto.randomUUID()
                : `assistant-${Date.now()}`;

            const drafts = dedupePendingActions(pendingActionsForTurnRef.current);
            pendingActionsForTurnRef.current = [];

            // 这一轮是否已经在流式气泡里逐字显示过？是的话就不能再挂给打字机，
            // 否则 done 事件到达时流式气泡被卸载、新消息从第 0 个字符重新播放，
            // 用户看到的就是"说完之后清空重说一遍"。
            const alreadyStreamed = (streamingByConversationIdRef.current[conversationId] ?? "").length > 0;

            setStreamingByConversationId((previous) => removeKey(previous, conversationId));
            if (!alreadyStreamed) {
              setAnimatingMessageIds((previous) => new Set(previous).add(assistantMessageId));
            }

            setMessagesByConversationId((previous) => ({
              ...previous,
              [conversationId]: [
                ...(previous[conversationId] ?? []),
                {
                  id: assistantMessageId,
                  role: "assistant",
                  text: assistantText,
                  createdAt: new Date().toISOString(),
                  pendingActions: drafts.length > 0 ? drafts : undefined,
                },
              ],
            }));
          },
          onComplete: () => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            persistConversation(conversationId);
            invalidateDataAfterChat();
          },
          onError: (errorMessage) => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStreamingByConversationId((previous) => removeKey(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            persistConversation(conversationId);
            const fallbackMessage: ChatMessage = {
              id:
                typeof crypto !== "undefined" && "randomUUID" in crypto
                  ? crypto.randomUUID()
                  : `assistant-${Date.now()}`,
              role: "assistant",
              text: `无法获取回复：${errorMessage}`,
            };
            setMessagesByConversationId((previous) => ({
              ...previous,
              [conversationId]: [...(previous[conversationId] ?? []), fallbackMessage],
            }));
          },
          onAbort: () => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            // Persist whatever we captured so far (user message + partial history);
            // the debounced saver is dropped on unmount, so flush here to avoid loss.
            persistConversation(conversationId);
          },
        });
      });
    },
    [
      activeConversationId,
      interruptReply,
      invalidateDataAfterChat,
      messagesByConversationId,
      persistConversation,
      requestAssistantReply,
    ],
  );

  /**
   * 点「停止」：停掉服务端生成，并把已经流出的半截回复固化成一条消息，
   * 否则那段文字会随着流式气泡一起消失。
   */
  const handleStopReply = useCallback(() => {
    const conversationId = activeConversationId;
    const partialText = streamingByConversationIdRef.current[conversationId] ?? "";

    interruptReply(conversationId);

    if (!partialText.trim()) {
      return;
    }

    const assistantMessageId =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `assistant-${Date.now()}`;

    setStreamingByConversationId((previous) => removeKey(previous, conversationId));
    streamingByConversationIdRef.current = removeKey(
      streamingByConversationIdRef.current,
      conversationId,
    );
    setMessagesByConversationId((previous) => ({
      ...previous,
      [conversationId]: [
        ...(previous[conversationId] ?? []),
        {
          id: assistantMessageId,
          role: "assistant",
          text: partialText,
          createdAt: new Date().toISOString(),
        },
      ],
    }));
  }, [activeConversationId, interruptReply]);

  const handleSendVoiceMessage = useCallback(
    (audioBlob: Blob) => {
      const conversationId = activeConversationId;

      const priorMessages: ChatCompletionMessagePayload[] = (messagesByConversationId[conversationId] ?? [])
        .filter((message) => message.text.trim().length > 0)
        .map((message) => ({
          role: message.role,
          content: message.text.trim(),
          timestamp: message.createdAt,
        }));

      setRepliesInFlight((previous) => ({
        ...previous,
        [conversationId]: (previous[conversationId] ?? 0) + 1,
      }));

      queueMicrotask(() => {
        void sendVoiceMessage(conversationId, audioBlob, priorMessages, {
          onSuccess: ({ transcript, content }) => {
            const userMessageId =
              typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `msg-${Date.now()}`;
            const assistantMessageId =
              typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `assistant-${Date.now()}`;

            setAnimatingMessageIds((previous) => new Set(previous).add(assistantMessageId));

            setMessagesByConversationId((previous) => ({
              ...previous,
              [conversationId]: [
                ...(previous[conversationId] ?? []),
                {
                  id: userMessageId,
                  role: "user",
                  text: transcript,
                },
                {
                  id: assistantMessageId,
                  role: "assistant",
                  text: content,
                  isPlainText: true,
                },
              ],
            }));
          },
          onComplete: () => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            persistConversation(conversationId);
            invalidateDataAfterChat();
          },
          onError: (errorMessage) => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            persistConversation(conversationId);
            const fallbackMessage: ChatMessage = {
              id:
                typeof crypto !== "undefined" && "randomUUID" in crypto
                  ? crypto.randomUUID()
                  : `assistant-${Date.now()}`,
              role: "assistant",
              text: `语音对话失败：${errorMessage}`,
            };
            setMessagesByConversationId((previous) => ({
              ...previous,
              [conversationId]: [...(previous[conversationId] ?? []), fallbackMessage],
            }));
          },
          onAbort: () => {
            setRepliesInFlight((previous) => decrementInFlightCount(previous, conversationId));
            setStatusByConversationId((previous) => removeKey(previous, conversationId));
            persistConversation(conversationId);
          },
        });
      });
    },
    [activeConversationId, invalidateDataAfterChat, messagesByConversationId, persistConversation, sendVoiceMessage],
  );

  const handleAddConversation = useCallback(() => {
    const newId = createConversationId();
    if (enterAnimationClearTimeoutRef.current !== null) {
      clearTimeout(enterAnimationClearTimeoutRef.current);
    }
    setEnteringConversationId(newId);
    enterAnimationClearTimeoutRef.current = setTimeout(() => {
      setEnteringConversationId(null);
      enterAnimationClearTimeoutRef.current = null;
    }, CHAT_ENTER_ANIMATION_CLEAR_MS);

    // 标题在 updater 里按"当前已用的最大编号 +1"取号：用 conversations.length+1
    // 会在关掉中间某个 Tab 后重复（剩 对话1、对话3 时新建又得到 对话3）。
    setConversations((previous) => [...previous, { id: newId, title: nextConversationTitle(previous) }]);
    setDraftsByConversationId((previous) => ({ ...previous, [newId]: "" }));
    setMessagesByConversationId((previous) => ({ ...previous, [newId]: [] }));
    setActiveConversationId(newId);
  }, []);

  /** Shared tab-list surgery used by both close-tab and delete paths. */
  const removeTabEntry = useCallback(
    (
      conversationId: string,
      indexToRemove: number,
      nextList: ConversationTab[],
      { abortStreaming }: { abortStreaming: boolean },
    ) => {
      setConversations(nextList);

      setDraftsByConversationId((previous) => {
        const nextDrafts = { ...previous };
        delete nextDrafts[conversationId];
        return nextDrafts;
      });

      setMessagesByConversationId((previous) => {
        const nextMessages = { ...previous };
        delete nextMessages[conversationId];
        return nextMessages;
      });

      setStreamingByConversationId((previous) => removeKey(previous, conversationId));

      if (abortStreaming) {
        abortChatForConversation(conversationId);
        abortVoiceForConversation(conversationId);
      }

      if (conversationId === activeConversationId) {
        const fallbackIndex = Math.min(indexToRemove, nextList.length - 1);
        const fallbackId = nextList[fallbackIndex]?.id ?? nextList[0]?.id;
        if (fallbackId) {
          setActiveConversationId(fallbackId);
        }
      }
    },
    [abortChatForConversation, abortVoiceForConversation, activeConversationId],
  );

  /** Tab X = close the tab only. Never deletes backend data; the conversation
   *  stays listed in the left history and can be reopened (server transcript
   *  lazy-restores on select). */
  const handleCloseTab = useCallback(
    (conversationId: string) => {
      if (conversations.length <= 1) {
        return;
      }
      const indexToRemove = conversations.findIndex((c) => c.id === conversationId);
      if (indexToRemove === -1) {
        return;
      }
      const nextList = conversations.filter((c) => c.id !== conversationId);
      // Write out anything the debounce still owes BEFORE the transcript is
      // dropped, otherwise a reply that finished <800ms ago never reaches the DB.
      flushConversationSave(conversationId);
      // Defer the transcript/message cleanup to after the fallback tab mounts so
      // the panel doesn't flash an empty thread while the new active tab loads.
      removeTabEntry(conversationId, indexToRemove, nextList, { abortStreaming: true });
    },
    [conversations, flushConversationSave, removeTabEntry],
  );

  /** Real delete: used only from the sidebar history `...` menu with confirmation. */
  const handleDeleteConversation = useCallback(
    (conversationId: string) => {
      const indexToRemove = conversations.findIndex((c) => c.id === conversationId);
      const wasPersisted = persistedConversationIdsRef.current.has(conversationId);

      // 1) 服务端有行就必须真的 DELETE，否则刷新后它又回来 —— 这正是"新建的
      //    空会话删不掉"的直接原因（本地删了、库里还在）。
      if (wasPersisted) {
        conversationsApi.deleteConversation(conversationId).catch(() => {
          // Backend unavailable — keep the local state consistent anyway.
        });
      }

      // 2) 本地簿记总是清掉，不管服务端有没有这一行。旧实现只在 wasPersisted
      //    时才删标记，未落库的 Tab 会残留成"删了还在"的幽灵项。
      cancelScheduledSave(conversationId);
      abortChatForConversation(conversationId);
      abortVoiceForConversation(conversationId);
      setPersistedConversationIds((previous) => {
        if (!previous.has(conversationId)) {
          return previous;
        }
        const next = new Set(previous);
        next.delete(conversationId);
        return next;
      });
      persistedConversationIdsRef.current.delete(conversationId);
      setHistoryConversations((previous) => previous.filter((item) => item.id !== conversationId));

      // 3) 只存在于历史列表（Tab 已关闭）时，删掉这一行即可，别动当前 Tab。
      if (indexToRemove === -1) {
        return;
      }

      if (conversations.length <= 1) {
        // 最后一个 Tab：换一条全新会话（新 id！沿用旧 id 会继续命中旧行），
        // 但历史列表里的其它会话必须保留 —— 旧实现把它整片清空，用户看到的
        // 就是"删一个，剩下全没了 / 名字变回对话 1"。
        const freshId = createConversationId();
        setConversations([{ id: freshId, title: "对话 1" }]);
        setDraftsByConversationId({ [freshId]: "" });
        setMessagesByConversationId({});
        setStreamingByConversationId({});
        setRepliesInFlight({});
        setStatusByConversationId({});
        setActiveConversationId(freshId);
        return;
      }

      const kept = conversations.filter((c) => c.id !== conversationId);
      removeTabEntry(conversationId, indexToRemove, kept, { abortStreaming: false });
    },
    [
      abortChatForConversation,
      abortVoiceForConversation,
      cancelScheduledSave,
      conversations,
      conversationsApi,
      removeTabEntry,
    ],
  );

  // Sidebar history = persisted conversations (kept even when their tab is
  // closed), plus any open tabs that haven't been persisted yet so a fresh
  // thread remains visible while typing.
  const sidebarItems = useMemo(() => {
    const byId = new Map(historyConversations.map((item) => [item.id, item]));
    for (const conversation of conversations) {
      const existing = byId.get(conversation.id);
      if (existing) {
        if (conversation.title && conversation.title !== existing.title) {
          byId.set(conversation.id, { ...existing, title: conversation.title });
        }
      } else {
        byId.set(conversation.id, {
          id: conversation.id,
          title: conversation.title,
          persisted: persistedConversationIds.has(conversation.id),
        });
      }
    }
    return [...byId.values()];
  }, [conversations, historyConversations, persistedConversationIds]);

  return (
    <WorkspaceShell
      topBar={
        <div className="flex min-h-10 items-center gap-2 rounded-xl border border-white/25 bg-white/12 px-2 py-1.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)] backdrop-blur-md">
          <p id="chat-tabs-heading" className="sr-only">
            {CHAT_TABLIST_LABEL}
          </p>
          <button
            type="button"
            onClick={handleAddConversation}
            aria-label="新建对话"
            title="新建对话"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/25 bg-white/20 text-zinc-800 shadow-sm transition hover:bg-white/35"
          >
            <IconPlus className="h-4 w-4" />
          </button>

          <div
            ref={chatTabListRef}
            className="flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto py-0.5"
            role="tablist"
            aria-labelledby="chat-tabs-heading"
          >
            {conversations.map((conversation, index) => {
              const isActive = conversation.id === activeConversationId;
              const accentClass = TAB_ICON_ACCENT_CLASSES[index % TAB_ICON_ACCENT_CLASSES.length];
              const tabEnterClass = enteringConversationId === conversation.id ? "animate-chat-tab-enter" : "";

              const tabIcon = (
                <span
                  className={`h-5 w-5 shrink-0 rounded-md ${accentClass} shadow-inner ring-1 ring-black/5`}
                  aria-hidden
                />
              );

              if (isActive && conversations.length > 1) {
                return (
                  <div
                    key={conversation.id}
                    role="presentation"
                    className={`flex h-8 min-w-0 shrink-0 items-stretch rounded-lg border border-white/35 bg-white/95 pl-2 shadow-sm ${tabEnterClass}`}
                  >
                    <button
                      type="button"
                      role="tab"
                      aria-selected
                      onClick={() => handleSelectConversation(conversation.id)}
                      className="flex min-w-0 flex-1 items-center gap-2 py-1 pr-1 text-left transition"
                    >
                      {tabIcon}
                      <span className="max-w-32 truncate text-[12px] font-medium text-zinc-800 sm:max-w-44">
                        {conversation.title}
                      </span>
                    </button>
                    <button
                      type="button"
                      aria-label={`关闭 ${conversation.title}`}
                      title="关闭标签页（不删除会话）"
                      className="flex w-8 shrink-0 items-center justify-center rounded-r-lg text-zinc-500 transition hover:bg-zinc-200/80 hover:text-zinc-900"
                      onClick={() => handleCloseTab(conversation.id)}
                    >
                      <IconCloseSmall className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              }

              return (
                <button
                  key={conversation.id}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  onClick={() => handleSelectConversation(conversation.id)}
                  className={`${
                    isActive
                      ? "flex h-8 min-w-0 shrink-0 items-center gap-2 rounded-lg border border-white/35 bg-white/95 px-2 shadow-sm transition"
                      : "flex h-8 min-w-0 shrink-0 items-center gap-2 rounded-lg border border-transparent bg-white/14 px-2 text-white/90 transition hover:bg-white/22"
                  } ${tabEnterClass}`.trim()}
                >
                  {tabIcon}
                  <span
                    className={
                      isActive
                        ? "max-w-32 truncate text-left text-[12px] font-medium text-zinc-800 sm:max-w-44"
                        : "max-w-32 truncate text-left text-[12px] font-medium text-white/95 sm:max-w-44"
                    }
                  >
                    {conversation.title}
                  </span>
                </button>
              );
            })}
          </div>

          <div className="flex shrink-0 items-center gap-1 border-l border-white/25 pl-2 sm:gap-1.5 sm:pl-3">
            <Link
              href="/life-pilot"
              aria-label="LifePilot"
              title="LifePilot"
              className={TOP_BAR_ICON_LINK_CLASS}
            >
              <IconCalendarSimple className="h-4 w-4" />
            </Link>
            <Link
              href="/tasks"
              aria-label="任务"
              title="任务"
              className={TOP_BAR_ICON_LINK_CLASS}
            >
              <IconCheckSquare className="h-4 w-4" />
            </Link>
            <Link
              href="/background-jobs"
              aria-label="后台任务"
              title="后台任务"
              className={TOP_BAR_ICON_LINK_CLASS}
            >
              <IconLayers className="h-4 w-4" />
            </Link>
            <Link
              href="/notifications"
              aria-label="通知"
              title="通知"
              className={`relative ${TOP_BAR_ICON_LINK_CLASS}`}
            >
              <IconBellNav className="h-4 w-4" />
              {topBarNotificationCount > 0 ? (
                <span className="absolute -right-1 -top-1 flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-rose-500 px-0.5 text-[10px] font-bold tabular-nums leading-none text-white shadow ring-2 ring-black/30">
                  {topBarNotificationCount > 9 ? "9+" : topBarNotificationCount}
                </span>
              ) : null}
            </Link>
          </div>

          <div ref={moreMenuContainerRef} className="relative shrink-0">
            <button
              type="button"
              id="glass-workspace-more-trigger"
              aria-label="工作区菜单"
              title="工作区菜单"
              aria-expanded={isMoreMenuOpen}
              aria-haspopup="true"
              aria-controls={WORKSPACE_MORE_MENU_DOM_ID}
              onClick={() => setIsMoreMenuOpen((open) => !open)}
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/25 bg-white/14 text-white/85 shadow-sm transition hover:bg-white/22 hover:text-white"
            >
              <IconDotsHorizontal className="h-5 w-5" />
            </button>
            {isMoreMenuOpen ? (
              <div
                id={WORKSPACE_MORE_MENU_DOM_ID}
                role="menu"
                aria-labelledby="glass-workspace-more-trigger"
                className="absolute right-0 top-full z-50 mt-1.5 min-w-54 overflow-hidden rounded-xl border border-white/20 bg-zinc-950/95 py-1 shadow-xl ring-1 ring-black/40 backdrop-blur-md"
              >
                {user?.email ? (
                  <p
                    className="truncate border-b border-white/10 px-3 py-2 text-[11px] leading-snug text-white/55"
                    role="none"
                  >
                    已登录为
                    <span className="mt-0.5 block truncate font-medium text-white/80">{user.email}</span>
                  </p>
                ) : null}
                <Link
                  href="/life-pilot"
                  role="menuitem"
                  className={WORKSPACE_MENU_LINK_CLASS}
                  onClick={() => setIsMoreMenuOpen(false)}
                >
                  <IconLayers className="h-4 w-4 shrink-0 text-white/70" />
                  LifePilot
                </Link>
                <Link
                  href="/tasks"
                  role="menuitem"
                  className={WORKSPACE_MENU_LINK_CLASS}
                  onClick={() => setIsMoreMenuOpen(false)}
                >
                  <IconCheckSquare className="h-4 w-4 shrink-0 text-white/70" />
                  任务
                </Link>
                <Link
                  href="/notifications"
                  role="menuitem"
                  className={WORKSPACE_MENU_LINK_CLASS}
                  onClick={() => setIsMoreMenuOpen(false)}
                >
                  <IconBellNav className="h-4 w-4 shrink-0 text-white/70" />
                  通知
                </Link>
                <Link
                  href="/background-jobs"
                  role="menuitem"
                  className={WORKSPACE_MENU_LINK_CLASS}
                  onClick={() => setIsMoreMenuOpen(false)}
                >
                  <IconLayers className="h-4 w-4 shrink-0 text-white/70" />
                  后台任务
                </Link>
                <div className="my-1 h-px bg-white/10" role="separator" />
                <button
                  type="button"
                  role="menuitem"
                  className={WORKSPACE_MENU_LOGOUT_CLASS}
                  disabled={isLogoutInFlight}
                  onClick={() => {
                    void handleWorkspaceLogout();
                  }}
                >
                  {isLogoutInFlight ? "正在退出…" : "退出登录"}
                </button>
              </div>
            ) : null}
          </div>
        </div>
      }
    >
      <div className="flex min-h-0 flex-1 gap-2">
        <ConversationSidebar
          items={sidebarItems}
          activeConversationId={activeConversationId}
          onSelect={handleSelectConversation}
          onNewChat={handleAddConversation}
          onDelete={handleDeleteConversation}
          isExpanded={isChatSidebarExpanded}
          onToggleExpanded={() => setIsChatSidebarExpanded((value) => !value)}
        />

        <div
          key={activeConversationId}
          className={`flex min-h-0 flex-1 flex-col overflow-hidden overscroll-contain ${
            enteringConversationId === activeConversationId ? "animate-chat-panel-enter" : ""
          }`}
        >
            <div className="flex min-h-0 flex-1 flex-col rounded-xl bg-black/40 px-2 pt-3 backdrop-blur-sm sm:px-4 lg:pt-4">
            <div
              className={`flex flex-col items-center overflow-hidden transition-all duration-700 ease-in-out ${
                hasConversationMessages
                  ? "pointer-events-none max-h-0 min-h-0 shrink-0 scale-95 opacity-0 blur-[2px]"
                  : "flex flex-1 shrink-0 flex-col justify-center gap-10 py-8"
              }`}
            >
              <div className="relative mx-auto shrink-0">
                <Image
                  src="/assets/orb.png"
                  alt=""
                  width={152}
                  height={152}
                  priority
                  aria-hidden
                  className="h-36 w-36 object-contain drop-shadow-[0_0_28px_rgba(168,85,247,0.55)] sm:h-40 sm:w-40"
                />
              </div>
  
              <h1 className="text-center text-xl font-medium tracking-tight text-white sm:text-2xl">
                今天 LifePilot 能帮你做什么？
              </h1>
              <p className="max-w-lg px-4 text-center text-sm text-white/55">
                点按一句起始提示即可填入输入框。LifePilot 会协调邮件、日历、会议、记忆和生活服务。
              </p>
  
              <div className="flex w-full max-w-xl flex-wrap items-center justify-center gap-3">
                <button
                  type="button"
                  onClick={() => applyStarterPromptToComposer(STARTER_PROMPT_GMAIL_SUMMARY)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
                >
                  <IconMail className="h-4 w-4 shrink-0 text-white/85" />
                  邮件整理
                </button>
                <button
                  type="button"
                  onClick={() => applyStarterPromptToComposer(STARTER_PROMPT_CALENDAR_SNAPSHOT)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
                >
                  <IconCalendarSimple className="h-4 w-4 shrink-0 text-white/85" />
                  日历概览
                </button>
                <button
                  type="button"
                  onClick={() => applyStarterPromptToComposer(STARTER_PROMPT_MEETING_EXTRACT)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
                >
                  <IconCalendarSimple className="h-4 w-4 shrink-0 text-white/85" />
                  提取邀请
                </button>
                <button
                  type="button"
                  onClick={() => applyStarterPromptToComposer(STARTER_PROMPT_LIFE_CONTEXT)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
                >
                  <IconLayers className="h-4 w-4 shrink-0 text-white/85" />
                  生活场景
                </button>
                <button
                  type="button"
                  onClick={() => applyStarterPromptToComposer(STARTER_PROMPT_WORKSPACE_CAPABILITIES)}
                  className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-black/25 px-4 py-2.5 text-sm text-white/95 backdrop-blur-md transition hover:bg-black/35"
                >
                  <IconLayers className="h-4 w-4 shrink-0 text-white/85" />
                  规划今天
                </button>
              </div>
            </div>
  
            <div
              ref={messagesScrollRef}
              className={`flex min-h-0 flex-col gap-2 overflow-y-auto overscroll-contain px-1 pb-2 transition-all duration-700 ease-in-out ${
                hasConversationMessages
                  ? "flex-1 opacity-100"
                  : "max-h-0 shrink-0 overflow-hidden opacity-0"
              }`}
              aria-live="polite"
            >
              <div className="flex-1" />
              {messagesForActiveConversation.map((message, index) => {
                const isLatest = index === messagesForActiveConversation.length - 1;
                const isUser = message.role === "user";
  
                if (!isUser) {
                  return (
                    <div
                      key={message.id}
                      className={isLatest ? "animate-chat-message-enter" : ""}
                    >
                      <AssistantMessage
                        messageId={message.id}
                        text={message.text}
                        shouldAnimate={animatingMessageIds.has(message.id)}
                        onTypingComplete={handleTypingComplete}
                        isPlainText={message.isPlainText === true}
                      />
                      {(message.pendingActions ?? []).map((action) => (
                        <PendingActionCard
                          key={action.id}
                          action={action}
                          onResult={(settled) => {
                            // 把内存里的快照也标成终态：否则切走再切回来，
                            // 这张卡又变回"等待确认"，用户会重复确认。
                            // 不能因为改成终态就把卡片从列表里过滤掉 —— 那样
                            // 用户点完"确认执行"只会看到卡片凭空消失，看不到
                            // "已发送"。终态由卡片自己渲染（内部的 inFlight 锁
                            // 保证同一次点击不会重复提交）。
                            setMessagesByConversationId((previous) => {
                              const thread = previous[activeConversationId] ?? [];
                              if (thread.length === 0) {
                                return previous;
                              }
                              return {
                                ...previous,
                                [activeConversationId]: thread.map((item) =>
                                  item.id === message.id && item.pendingActions
                                    ? {
                                        ...item,
                                        pendingActions: item.pendingActions.map((entry) =>
                                          entry.id === action.id ? { ...entry, status: settled } : entry,
                                        ),
                                      }
                                    : item,
                                ),
                              };
                            });
                            invalidateDataAfterChat();
                          }}
                        />
                      ))}
                    </div>
                  );
                }
  
                return (
                  <div
                    key={message.id}
                    className={`flex px-1 sm:px-2 justify-end ${isLatest ? "animate-chat-message-enter" : ""}`}
                  >
                    <div className="max-w-[min(85%,28rem)] rounded-[1.25rem] rounded-br-md bg-[#7c3aed]/92 px-4 py-2.5 text-left text-[15px] leading-snug text-white shadow-[0_8px_24px_-8px_rgba(0,0,0,0.45)]">
                      {message.text}
                    </div>
                  </div>
                );
              })}
  
              {(() => {
                const streamingText = streamingByConversationId[activeConversationId];
                if (streamingText && streamingText.length > 0) {
                  return (
                    <div
                      key="streaming-assistant"
                      className="animate-chat-message-enter"
                    >
                      <StreamingAssistantMessage text={streamingText} />
                    </div>
                  );
                }
                return null;
              })()}
  
              {(repliesInFlight[activeConversationId] ?? 0) > 0 && (
                <div className="flex items-center gap-2.5 px-2 py-3 sm:px-3 animate-chat-message-enter">
                  <Image
                    src="/assets/orb.png"
                    alt=""
                    width={24}
                    height={24}
                    aria-hidden
                    className="h-6 w-6 animate-orb-bounce object-contain drop-shadow-[0_0_12px_rgba(168,85,247,0.6)]"
                  />
                  <span
                    key={statusByConversationId[activeConversationId] ?? "thinking"}
                    className="animate-pulse text-sm font-medium text-white/60 transition-all duration-300"
                  >
                    {statusByConversationId[activeConversationId] ?? "正在思考..."}
                  </span>
                </div>
              )}
            </div>
          </div>
  
          <div className="shrink-0 px-2 pb-4 pt-1 sm:px-4">
            <div className="mx-auto flex w-full max-w-2xl justify-center">
              <ChatComposer
                draftValue={activeDraft}
                onDraftChange={setActiveDraft}
                isMinimal={hasConversationMessages}
                isStreaming={isActiveReplyStreaming}
                onStop={handleStopReply}
                onSend={handleSendChatMessage}
                onVoiceMessageSend={handleSendVoiceMessage}
                onConnectMail163={connectMail163}
                onConnectNotion={connectNotion}
                onConnectCalendar={connectCalendar}
                onDisconnectMail163={disconnectMail163}
                onDisconnectNotion={disconnectNotion}
                onDisconnectCalendar={disconnectCalendar}
                integrationStatus={{
                  isMail163Connected: mail163Status.isConnected,
                  isNotionConnected: notionStatus.isConnected,
                  isCalendarConnected: calendarStatus.isConnected,
                }}
              />
            </div>
          </div>
      </div>
      </div>
    </WorkspaceShell>
  );
}
