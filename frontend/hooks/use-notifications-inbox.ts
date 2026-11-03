"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  NOTIFICATIONS_INBOX_QUERY_KEY,
  NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY,
} from "@/lib/notifications-query-keys";
import { authenticatedFetch } from "@/lib/authenticated-fetch";
import {
  NOTIFICATION_CATEGORY_AGENT,
  NOTIFICATION_CATEGORY_BACKGROUND_JOB,
  NOTIFICATION_CATEGORY_CALENDAR,
  NOTIFICATION_CATEGORY_EMAIL,
  NOTIFICATION_CATEGORY_REMINDER,
  NOTIFICATION_CATEGORY_SYSTEM,
  type NotificationCategory,
  type NotificationItem,
} from "@/lib/mock-notifications";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

interface ApiNotificationRow {
  readonly id: string;
  readonly title: string;
  readonly body: string;
  readonly category: string;
  readonly is_read: boolean;
  readonly created_at_iso: string;
  readonly reminder_id?: string | null;
  readonly task_id?: string | null;
}

function mapCategory(raw: string): NotificationCategory {
  switch (raw) {
    case NOTIFICATION_CATEGORY_EMAIL:
      return NOTIFICATION_CATEGORY_EMAIL;
    case NOTIFICATION_CATEGORY_CALENDAR:
      return NOTIFICATION_CATEGORY_CALENDAR;
    case NOTIFICATION_CATEGORY_AGENT:
      return NOTIFICATION_CATEGORY_AGENT;
    case NOTIFICATION_CATEGORY_SYSTEM:
      return NOTIFICATION_CATEGORY_SYSTEM;
    case NOTIFICATION_CATEGORY_BACKGROUND_JOB:
      return NOTIFICATION_CATEGORY_BACKGROUND_JOB;
    case NOTIFICATION_CATEGORY_REMINDER:
      return NOTIFICATION_CATEGORY_REMINDER;
    default:
      return NOTIFICATION_CATEGORY_SYSTEM;
  }
}

function mapRow(row: ApiNotificationRow): NotificationItem {
  return {
    id: row.id,
    title: row.title,
    body: row.body,
    category: mapCategory(row.category),
    isRead: row.is_read,
    createdAtIso: row.created_at_iso,
    reminderId: row.reminder_id ?? null,
    taskId: row.task_id ?? null,
  };
}

async function fetchNotificationsInbox(): Promise<readonly NotificationItem[]> {
  const response = await authenticatedFetch(`${API_BASE_URL}/notifications`);
  if (!response.ok) {
    throw new Error("无法加载通知。");
  }
  const data = (await response.json()) as ApiNotificationRow[];
  return data.map(mapRow);
}

function invalidateNotificationQueries(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_INBOX_QUERY_KEY });
  void queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY });
}

const DEFAULT_POLL_MS = 20_000;

export interface CreatedTaskFromNotification {
  readonly task_id: string;
  readonly title: string;
  readonly due_at: string | null;
  readonly created: boolean;
}

interface ReadSnapshot {
  readonly previousInbox: readonly NotificationItem[] | undefined;
  readonly previousCount: number | undefined;
}

export function useNotificationsInbox({
  pollIntervalMs = DEFAULT_POLL_MS,
}: { pollIntervalMs?: number } = {}) {
  const queryClient = useQueryClient();

  const inboxQuery = useQuery({
    queryKey: NOTIFICATIONS_INBOX_QUERY_KEY,
    queryFn: fetchNotificationsInbox,
    // The toast host sits in the root layout, so the inbox has to stay live on
    // every page — not only while the notifications tab renders it.
    refetchInterval: pollIntervalMs,
    retry: false,
  });

  const markAsReadMutation = useMutation({
    mutationFn: async (notificationId: string) => {
      const response = await authenticatedFetch(`${API_BASE_URL}/notifications/${notificationId}/read`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error("标记已读失败。");
      }
    },
    onMutate: async (notificationId: string): Promise<ReadSnapshot> => {
      // Flip the row instantly; the request only confirms it server-side.
      await queryClient.cancelQueries({ queryKey: NOTIFICATIONS_INBOX_QUERY_KEY });
      const previousInbox = queryClient.getQueryData<readonly NotificationItem[]>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
      );
      const previousCount = queryClient.getQueryData<number>(NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY);
      const wasUnread = previousInbox?.some((item) => item.id === notificationId && !item.isRead);

      queryClient.setQueryData<readonly NotificationItem[]>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
        (previous) =>
          previous?.map((item) =>
            item.id === notificationId ? { ...item, isRead: true } : item,
          ),
      );
      if (wasUnread && typeof previousCount === "number") {
        queryClient.setQueryData<number>(
          NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY,
          Math.max(0, previousCount - 1),
        );
      }
      return { previousInbox, previousCount };
    },
    onError: (_error, _notificationId, context) => {
      if (!context) {
        return;
      }
      queryClient.setQueryData(NOTIFICATIONS_INBOX_QUERY_KEY, context.previousInbox);
      if (context.previousCount !== undefined) {
        queryClient.setQueryData(NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY, context.previousCount);
      }
    },
    onSettled: () => {
      invalidateNotificationQueries(queryClient);
    },
  });

  const markAllAsReadMutation = useMutation({
    mutationFn: async () => {
      const response = await authenticatedFetch(`${API_BASE_URL}/notifications/read-all`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error("全部标记已读失败。");
      }
    },
    onMutate: async (): Promise<ReadSnapshot> => {
      await queryClient.cancelQueries({ queryKey: NOTIFICATIONS_INBOX_QUERY_KEY });
      const previousInbox = queryClient.getQueryData<readonly NotificationItem[]>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
      );
      const previousCount = queryClient.getQueryData<number>(NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY);

      queryClient.setQueryData<readonly NotificationItem[]>(
        NOTIFICATIONS_INBOX_QUERY_KEY,
        (previous) => previous?.map((item) => ({ ...item, isRead: true })),
      );
      queryClient.setQueryData<number>(NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY, 0);
      return { previousInbox, previousCount };
    },
    onError: (_error, _variables, context) => {
      if (!context) {
        return;
      }
      queryClient.setQueryData(NOTIFICATIONS_INBOX_QUERY_KEY, context.previousInbox);
      if (context.previousCount !== undefined) {
        queryClient.setQueryData(NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY, context.previousCount);
      }
    },
    onSettled: () => {
      invalidateNotificationQueries(queryClient);
    },
  });

  const convertToTaskMutation = useMutation({
    mutationFn: async (notificationId: string): Promise<CreatedTaskFromNotification> => {
      const response = await authenticatedFetch(
        `${API_BASE_URL}/notifications/${notificationId}/create-task`,
        { method: "POST" },
      );
      if (!response.ok) {
        throw new Error("转成任务失败。");
      }
      return (await response.json()) as CreatedTaskFromNotification;
    },
    onSettled: () => {
      invalidateNotificationQueries(queryClient);
    },
  });

  const loadError =
    inboxQuery.isError && inboxQuery.error instanceof Error
      ? inboxQuery.error.message
      : inboxQuery.isError
        ? "无法加载通知。"
        : null;

  return {
    items: inboxQuery.data ?? [],
    isLoading: inboxQuery.isPending,
    loadError,
    refresh: inboxQuery.refetch,
    markAsRead: (notificationId: string) => {
      markAsReadMutation.mutate(notificationId);
    },
    markAllAsRead: () => {
      markAllAsReadMutation.mutate();
    },
    convertToTask: (notificationId: string) => convertToTaskMutation.mutateAsync(notificationId),
  };
}
