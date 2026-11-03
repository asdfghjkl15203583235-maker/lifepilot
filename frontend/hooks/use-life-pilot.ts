"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export interface Reminder {
  id: string;
  task_id: string | null;
  title: string;
  note: string | null;
  remind_at: string;
  trigger_type: string;
  status: string;
  sent_at: string | null;
  created_at: string;
}

export interface LifeServiceRecord {
  id: string;
  service_name: string;
  status: string;
  result: Record<string, unknown> | null;
  idempotency_key: string | null;
}

export interface MemoryItem {
  id: string;
  text: string;
  category: string;
  is_active: boolean;
  created_at: string | null;
}

export interface UserMemory {
  items: MemoryItem[];
  services: LifeServiceRecord[];
  reminders: Reminder[];
}

export const LIFE_PILOT_QUERY_KEY = ["life-pilot", "overview"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function mapMemory(raw: unknown): MemoryItem {
  const row = isRecord(raw) ? raw : {};
  return {
    id: String(row.id ?? ""),
    text: String(row.text ?? ""),
    category: String(row.category ?? "preference"),
    is_active: row.is_active !== false,
    created_at: row.created_at == null ? null : String(row.created_at),
  };
}

async function getBundle(): Promise<UserMemory> {
  const [servicesRes, remindersRes, memoriesRes] = await Promise.all([
    authenticatedFetch(`${API_BASE_URL}/life-services`),
    authenticatedFetch(`${API_BASE_URL}/reminders`),
    authenticatedFetch(`${API_BASE_URL}/memory?limit=100`),
  ]);

  const services: LifeServiceRecord[] = servicesRes.ok
    ? ((await servicesRes.json()) as unknown[]).filter(isRecord).map((row) => ({
        id: String(row.id ?? ""),
        service_name: String(row.service_name ?? ""),
        status: String(row.status ?? ""),
        result: isRecord(row.result) ? row.result : null,
        idempotency_key: row.idempotency_key == null ? null : String(row.idempotency_key),
      }))
    : [];

  const reminders: Reminder[] = remindersRes.ok
    ? ((await remindersRes.json()) as unknown[]).filter(isRecord).map((row) => ({
        id: String(row.id ?? ""),
        task_id: row.task_id == null ? null : String(row.task_id),
        title: String(row.title ?? ""),
        note: row.note == null ? null : String(row.note),
        remind_at: String(row.remind_at ?? ""),
        trigger_type: String(row.trigger_type ?? "manual"),
        status: String(row.status ?? "pending"),
        sent_at: row.sent_at == null ? null : String(row.sent_at),
        created_at: String(row.created_at ?? ""),
      }))
    : [];

  // 软删除（is_active=false）的记录绝不允许出现在面板上。后端 GET /memory 已经
  // 过滤，这里再挡一次：前端缓存、旧接口、未来分页都可能把 inactive 行带进来，
  // 用户看到"已删的记忆还在"就会认为删除没生效。
  const memories: MemoryItem[] = memoriesRes.ok
    ? ((await memoriesRes.json()) as unknown[]).map(mapMemory).filter((item) => item.is_active)
    : [];

  return { items: memories, services, reminders };
}

export function useLifePilotBundle() {
  return useQuery({
    queryKey: LIFE_PILOT_QUERY_KEY,
    queryFn: getBundle,
    staleTime: 20_000,
  });
}

export function useLifeServiceMutation(service: "weather" | "map") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const response = await authenticatedFetch(`${API_BASE_URL}/life-services/${service}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `无法运行 ${service === "weather" ? "天气" : "地图"}查询。`);
      }
      return response.json() as Promise<LifeServiceRecord>;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LIFE_PILOT_QUERY_KEY });
    },
  });
}

export function useMemoryCreate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { text: string; category: string }) => {
      const response = await authenticatedFetch(`${API_BASE_URL}/memory`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error("无法保存记忆。");
      }
      return response.json() as Promise<MemoryItem>;
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: LIFE_PILOT_QUERY_KEY });
    },
  });
}

export function useMemoryDelete() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (memoryId: string) => {
      const response = await authenticatedFetch(`${API_BASE_URL}/memory/${memoryId}`, {
        method: "DELETE",
      });
      if (response.status !== 204) {
        throw new Error("无法删除记忆。");
      }
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: LIFE_PILOT_QUERY_KEY });
    },
  });
}