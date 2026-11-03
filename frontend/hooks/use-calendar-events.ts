"use client";

import { useMemo } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";
import type { Task } from "@/hooks/use-tasks";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export interface LocalCalendarEvent {
  id: string;
  uid: string;
  source_file: string | null;
  title: string;
  description: string | null;
  location: string | null;
  starts_at: string;
  ends_at: string | null;
  all_day: boolean;
}

export interface CalendarDayBundle {
  events: LocalCalendarEvent[];
  tasks: Task[];
}

export const CALENDAR_EVENTS_QUERY_KEY = ["calendar", "events"] as const;

function toIso(date: Date): string {
  return date.toISOString();
}

async function fetchCalendarEvents(start: Date, end: Date, enabled: boolean): Promise<CalendarDayBundle> {
  if (!enabled) return { events: [], tasks: [] };
  const url = new URL(`${API_BASE_URL}/calendar/events`);
  url.searchParams.set("start_at", toIso(start));
  url.searchParams.set("end_at", toIso(end));
  const res = await authenticatedFetch(url.toString());
  if (!res.ok) throw new Error(`日历请求失败（${res.status}）`);
  return res.json() as Promise<CalendarDayBundle>;
}

export async function syncLocalCalendar(): Promise<{
  status: string;
  imported: number;
  updated: number;
  skipped: number;
  files: number;
  errors: number;
}> {
  const res = await authenticatedFetch(`${API_BASE_URL}/calendar/sync-local`, {
    method: "POST",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `同步本地日历失败（${res.status}）`);
  }
  return res.json() as Promise<{
    status: string;
    imported: number;
    updated: number;
    skipped: number;
    files: number;
    errors: number;
  }>;
}

/** 以"今天本地 0 点"为窗口起点，保证 queryKey 在一整天内稳定（而非每次渲染毫秒级变化导致缓存失效）。 */
function todayStartLocal(): Date {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

export function useNextUpcoming() {
  const start = todayStartLocal();
  const end = new Date(start.getTime() + 30 * 86_400_000);
  const { data, isPending, isError } = useCalendarEvents([start, end] as const);
  return useMemo(() => {
    const candidates: Array<
      | { kind: "event"; id: string; title: string; startsAt: Date }
      | { kind: "task"; id: string; title: string; startsAt: Date }
    > = [];
    for (const event of data?.events ?? []) {
      const d = new Date(event.starts_at);
      if (d.getTime() >= Date.now()) candidates.push({ kind: "event", id: event.id, title: event.title, startsAt: d });
    }
    for (const task of data?.tasks ?? []) {
      // 已完成的任务不能再出现在"下一项"卡片上：勾选完成后它会立刻消失，
      // 而不是等到下一个有 due_at 的条目顶上来。
      if (task.status === "done") continue;
      if (task.due_at) {
        const d = new Date(task.due_at);
        if (d.getTime() >= Date.now()) candidates.push({ kind: "task", id: task.id, title: task.title, startsAt: d });
      }
    }
    candidates.sort((a, b) => a.startsAt.getTime() - b.startsAt.getTime());
    return { next: candidates[0] ?? null, isPending, isError };
  }, [data, isPending, isError]);
}

export function useCalendarEvents(DateRange: readonly [Date, Date]) {
  const [start, end] = DateRange;
  const enabled = !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime());
  return useQuery({
    queryKey: [...CALENDAR_EVENTS_QUERY_KEY, start.toISOString(), end.toISOString()],
    queryFn: () => fetchCalendarEvents(start, end, enabled),
    enabled,
  });
}

export function useSyncLocalCalendar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: syncLocalCalendar,
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: CALENDAR_EVENTS_QUERY_KEY });
    },
  });
}