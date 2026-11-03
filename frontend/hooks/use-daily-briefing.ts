"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export interface DailyBriefing {
  date: string;
  content: string;
}

export const DAILY_BRIEFING_QUERY_KEY = ["daily-briefing"] as const;

async function getBriefing(): Promise<DailyBriefing> {
  const response = await authenticatedFetch(`${API_BASE_URL}/dashboard/daily-briefing`);
  if (!response.ok) {
    throw new Error("无法加载今日晨报。");
  }
  return (await response.json()) as DailyBriefing;
}

export function useDailyBriefing() {
  return useQuery({
    queryKey: DAILY_BRIEFING_QUERY_KEY,
    queryFn: getBriefing,
    staleTime: 60_000,
  });
}

export function useRefreshBriefing() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const response = await authenticatedFetch(
        `${API_BASE_URL}/dashboard/daily-briefing?force_refresh=true`,
      );
      if (!response.ok) {
        throw new Error("无法刷新今日晨报。");
      }
      return (await response.json()) as DailyBriefing;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: DAILY_BRIEFING_QUERY_KEY });
    },
  });
}