"use client";

import { useQuery } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";
import { NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY } from "@/lib/notifications-query-keys";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

const DEFAULT_POLL_MS = 45_000;

async function fetchUnreadCount(): Promise<number> {
  const response = await authenticatedFetch(`${API_BASE_URL}/notifications/unread-count`);
  if (!response.ok) {
    throw new Error("未读通知数量请求失败。");
  }
  const data = (await response.json()) as { count: number };
  return data.count;
}

export function useNotificationUnreadCount(pollIntervalMs: number = DEFAULT_POLL_MS) {
  const unreadQuery = useQuery({
    queryKey: NOTIFICATIONS_UNREAD_COUNT_QUERY_KEY,
    queryFn: fetchUnreadCount,
    refetchInterval: pollIntervalMs,
    retry: false,
    throwOnError: false,
  });

  return {
    unreadCount: unreadQuery.data ?? 0,
    refreshUnreadCount: unreadQuery.refetch,
  };
}
