"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";
import {
  type BackgroundJob,
  type JobRun,
  type ActionType,
  ACTION_TYPE_CUSTOM,
} from "@/lib/mock-background-jobs";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export const BACKGROUND_JOBS_DATA_QUERY_KEY = ["background-jobs", "data"] as const;

interface BackgroundJobsBundle {
  readonly jobs: readonly BackgroundJob[];
  readonly runs: readonly JobRun[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function mapApiJob(row: unknown): BackgroundJob {
  if (!isRecord(row)) {
    return {
      id: "invalid",
      title: "无效的任务数据",
      instruction: "",
      scheduleDescription: "",
      timezoneLabel: "UTC",
      actions: [],
      status: "ended",
      nextRunIso: null,
      scheduleStartsAtIso: undefined,
      scheduleEndsAtIso: undefined,
      createdAtIso: "",
    };
  }
  return {
    id: String(row.id),
    title: String(row.title),
    instruction: String(row.instruction),
    scheduleDescription: String(row.schedule_description),
    timezoneLabel: String(row.timezone_label),
    actions: Array.isArray(row.actions)
      ? (row.actions as { action_type?: string; label?: string }[]).map((a) => {
          const t = a.action_type;
          const actionType: ActionType =
            t === "email" || t === "calendar" || t === "notion" || t === "custom" ? t : ACTION_TYPE_CUSTOM;
          return { actionType, label: String(a.label ?? "") };
        })
      : [],
    status: row.status as BackgroundJob["status"],
    nextRunIso: row.next_run_iso == null ? null : String(row.next_run_iso),
    scheduleStartsAtIso:
      row.schedule_starts_at_iso == null || row.schedule_starts_at_iso === ""
        ? undefined
        : String(row.schedule_starts_at_iso),
    scheduleEndsAtIso:
      row.schedule_ends_at_iso == null || row.schedule_ends_at_iso === ""
        ? undefined
        : String(row.schedule_ends_at_iso),
    createdAtIso: String(row.created_at_iso),
    endedAtIso: row.ended_at_iso == null ? undefined : String(row.ended_at_iso),
  };
}

function mapApiRun(row: unknown): JobRun {
  if (!isRecord(row)) {
    return {
      id: "invalid",
      jobId: "invalid",
      jobTitle: "无效任务",
      startedAtIso: new Date(0).toISOString(),
      finishedAtIso: new Date(0).toISOString(),
      outcome: "failed",
      detail: "无效数据",
      durationMs: 0,
      traces: [],
    };
  }
  const tracesRaw = row.traces;
  const traces: JobRun["traces"] = Array.isArray(tracesRaw)
    ? (tracesRaw.filter(isRecord).map((t) => ({
        id: String(t.id),
        agentLabel: String(t.agentLabel),
        stepKind: t.stepKind as JobRun["traces"][number]["stepKind"],
        title: String(t.title),
        detail: String(t.detail),
        atIso: String(t.atIso),
      })) as JobRun["traces"])
    : [];
  return {
    id: String(row.id),
    jobId: String(row.job_id),
    jobTitle: String(row.job_title),
    startedAtIso: String(row.started_at_iso),
    finishedAtIso: String(row.finished_at_iso),
    outcome: row.outcome as JobRun["outcome"],
    detail: String(row.detail),
    durationMs: Number(row.duration_ms) || 0,
    traces,
  };
}

export async function fetchBackgroundJobsBundle(): Promise<BackgroundJobsBundle> {
  const [jobsRes, runsRes] = await Promise.all([
    authenticatedFetch(`${API_BASE_URL}/background-jobs`),
    authenticatedFetch(`${API_BASE_URL}/background-jobs/runs?limit=200`),
  ]);
  if (!jobsRes.ok) {
    throw new Error(`任务请求失败（${jobsRes.status}）`);
  }
  if (!runsRes.ok) {
    throw new Error(`运行记录请求失败（${runsRes.status}）`);
  }
  const jobsRow = (await jobsRes.json()) as unknown;
  const runsRow = (await runsRes.json()) as unknown;
  return {
    jobs: Array.isArray(jobsRow) ? jobsRow.map(mapApiJob) : [],
    runs: Array.isArray(runsRow) ? runsRow.map(mapApiRun) : [],
  };
}

export function useBackgroundJobsQuery() {
  return useQuery({
    queryKey: BACKGROUND_JOBS_DATA_QUERY_KEY,
    queryFn: fetchBackgroundJobsBundle,
  });
}

function invalidateBackgroundJobsData(queryClient: ReturnType<typeof useQueryClient>) {
  return queryClient.invalidateQueries({ queryKey: BACKGROUND_JOBS_DATA_QUERY_KEY });
}

export function useStopBackgroundJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (jobId: string) => {
      const res = await authenticatedFetch(`${API_BASE_URL}/background-jobs/${jobId}/stop`, {
        method: "POST",
      });
      if (res.status === 404) {
        throw new Error("定时任务不存在。");
      }
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `停止失败（${res.status}）`);
      }
      return res.json() as Promise<Record<string, unknown>>;
    },
    onSettled: () => {
      void invalidateBackgroundJobsData(queryClient);
    },
  });
}

export function useDeleteBackgroundJobMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (jobId: string) => {
      const res = await authenticatedFetch(`${API_BASE_URL}/background-jobs/${jobId}`, {
        method: "DELETE",
      });
      if (res.status === 404) {
        throw new Error("定时任务不存在。");
      }
      if (res.status !== 204) {
        const text = await res.text();
        throw new Error(text || `删除失败（${res.status}）`);
      }
    },
    onSettled: () => {
      void invalidateBackgroundJobsData(queryClient);
    },
  });
}
