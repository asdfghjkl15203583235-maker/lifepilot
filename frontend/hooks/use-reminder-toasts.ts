"use client";

import { useRouter } from "next/navigation";
import { useEffect, useReducer, useRef } from "react";

import { useNotificationUnreadCount } from "@/hooks/use-notification-unread-count";
import { useNotificationsInbox } from "@/hooks/use-notifications-inbox";
import { authenticatedFetch } from "@/lib/authenticated-fetch";
import {
  NOTIFICATION_CATEGORY_EMAIL,
  NOTIFICATION_CATEGORY_REMINDER,
  type NotificationCategory,
  type NotificationItem,
} from "@/lib/mock-notifications";
import { NOTIFICATIONS_INBOX_QUERY_KEY, NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY } from "@/lib/notifications-query-keys";
import { useQueryClient } from "@tanstack/react-query";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

/** 同屏最多几张卡；多出来的排队，关一张补一张，不丢弃。 */
const MAX_VISIBLE_TOASTS = 5;

export interface ReminderToastItem {
  readonly notificationId: string;
  readonly reminderId: string | null;
  readonly taskId: string | null;
  readonly category: NotificationCategory;
  readonly title: string;
  readonly body: string;
  readonly createdAtIso: string;
}

interface ToastState {
  readonly visible: readonly ReminderToastItem[];
  readonly pending: readonly ReminderToastItem[];
}

type ToastAction =
  | { readonly type: "enqueue"; readonly items: readonly ReminderToastItem[] }
  | { readonly type: "remove"; readonly notificationId: string }
  | { readonly type: "clear" };

const EMPTY_TOAST_STATE: ToastState = { visible: [], pending: [] };

function toastReducer(state: ToastState, action: ToastAction): ToastState {
  switch (action.type) {
    case "enqueue": {
      // 新的排最前；排队区的补位顺序保持"先到先展示"。
      const combined = [...action.items, ...state.visible, ...state.pending];
      return {
        visible: combined.slice(0, MAX_VISIBLE_TOASTS),
        pending: combined.slice(MAX_VISIBLE_TOASTS),
      };
    }
    case "remove": {
      const remaining = state.visible.filter((toast) => toast.notificationId !== action.notificationId);
      const [promoted, ...restPending] = state.pending;
      return {
        visible: promoted ? [...remaining, promoted] : remaining,
        pending: restPending,
      };
    }
    case "clear":
      return EMPTY_TOAST_STATE;
    default:
      return state;
  }
}

function isToastCandidate(item: NotificationItem): boolean {
  if (item.isRead) {
    return false;
  }
  if (item.category === NOTIFICATION_CATEGORY_REMINDER) {
    return Boolean(item.reminderId);
  }
  // 新邮件提醒：不建立提醒行，所以没有 reminderId 也要弹。
  return item.category === NOTIFICATION_CATEGORY_EMAIL;
}

/** Newest first; stack grows downward in the bottom-right host. */
export function useReminderToasts(pollIntervalMs: number = 20_000) {
  const {
    items,
    isLoading,
    refresh,
    markAsRead,
    convertToTask: createTaskFromNotification,
  } = useNotificationsInbox({ pollIntervalMs });
  useNotificationUnreadCount(pollIntervalMs); // keep the bell count fresh with the same cadence

  const queryClient = useQueryClient();
  const router = useRouter();

  const [toastState, dispatch] = useReducer(toastReducer, EMPTY_TOAST_STATE);
  const seenIdsRef = useRef<ReadonlySet<string>>(new Set());
  const seededRef = useRef(false);

  useEffect(() => {
    if (isLoading || items.length === 0) {
      return;
    }
    const alerts = items.filter(isToastCandidate);

    // First successful load: seed the seen-set so pre-existing unread reminders
    // (created before this session) don't fire a toast burst on page open.
    // Only alerts that appear *later* (task just went due while the app is open)
    // should pop the toast.
    if (!seededRef.current) {
      seededRef.current = true;
      const seed = new Set([
        ...seenIdsRef.current,
        ...items.map((item) => item.id),
      ]);
      seenIdsRef.current = seed;
      return;
    }

    const fresh = alerts.filter((item) => !seenIdsRef.current.has(item.id));
    if (fresh.length === 0) {
      return;
    }
    const next: ReminderToastItem[] = fresh.map((item) => ({
      notificationId: item.id,
      reminderId: item.reminderId ?? null,
      taskId: item.taskId ?? null,
      category: item.category,
      title: item.title,
      body: item.body,
      createdAtIso: item.createdAtIso,
    }));
    seenIdsRef.current = new Set([...seenIdsRef.current, ...next.map((t) => t.notificationId)]);
    dispatch({ type: "enqueue", items: next });
  }, [items, isLoading]);

  const removeToast = (notificationId: string) => dispatch({ type: "remove", notificationId });

  const refreshNotificationQueries = () => {
    void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_INBOX_QUERY_KEY });
    void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY });
  };

  const dismiss = (notificationId: string) => {
    removeToast(notificationId);
  };

  /** 「关闭」：关掉弹窗并把这条通知标为已读，铃铛数字同步 -1。 */
  const acknowledge = (toast: ReminderToastItem) => {
    removeToast(toast.notificationId);
    markAsRead(toast.notificationId);
  };

  /** 「查看任务」：跳到任务详情，同时视为已读到。 */
  const openTask = (toast: ReminderToastItem) => {
    removeToast(toast.notificationId);
    markAsRead(toast.notificationId);
    if (toast.taskId) {
      router.push(`/tasks/${toast.taskId}`);
    }
  };

  /** 「转成任务」：变成看板任务（默认明天 9:00 到期），建完跳到任务详情。 */
  const convertToTask = async (toast: ReminderToastItem) => {
    try {
      const created = await createTaskFromNotification(toast.notificationId);
      removeToast(toast.notificationId);
      markAsRead(toast.notificationId);
      router.push(`/tasks/${created.task_id}`);
    } catch {
      // 失败时保留弹窗，让用户能再点一次。
    }
  };

  const snooze = async (toast: ReminderToastItem, minutes: number) => {
    try {
      if (toast.reminderId) {
        const response = await authenticatedFetch(`${API_BASE_URL}/reminders/${toast.reminderId}/snooze`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ minutes }),
        });
        if (!response.ok) {
          throw new Error(`稍后提醒失败（${response.status}）`);
        }
      }
    } catch {
      // Keep the toast visible so the user can still dismiss it.
      return;
    }
    removeToast(toast.notificationId);
    refreshNotificationQueries();
  };

  const completeTask = async (toast: ReminderToastItem) => {
    try {
      if (toast.taskId) {
        const response = await authenticatedFetch(`${API_BASE_URL}/tasks/${toast.taskId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "done" }),
        });
        if (!response.ok) {
          throw new Error(`完成任务失败（${response.status}）`);
        }
      }
    } catch {
      // Keep the toast visible so the user can still dismiss it.
      return;
    }
    removeToast(toast.notificationId);
    refreshNotificationQueries();
  };

  const dismissAll = () => dispatch({ type: "clear" });

  return {
    toasts: toastState.visible,
    queuedCount: toastState.pending.length,
    dismiss,
    acknowledge,
    openTask,
    convertToTask,
    snooze,
    completeTask,
    dismissAll,
    refresh,
  };
}
