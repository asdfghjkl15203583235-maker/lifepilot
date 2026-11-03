"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

export type TaskStatus = "not_started" | "in_progress" | "done";

export type TaskPriority = "low" | "medium" | "high" | "urgent";

export type TaskSource = "user" | "system";

export const AGENT_OPTIONS = [
  { value: "orchestrator", label: "规划器" },
  { value: "email_agent", label: "邮件代理" },
  { value: "calendar_agent", label: "日历代理" },
  { value: "notion_agent", label: "Notion 代理" },
  { value: "life_agent", label: "生活代理" },
  { value: "direct", label: "直接回复" },
] as const;

export type AgentOption = (typeof AGENT_OPTIONS)[number]["value"];

export interface Task {
  id: string;
  user_id: string;
  title: string;
  description: string | null;
  status: TaskStatus;
  project: string | null;
  assigned_agent: string | null;
  priority: TaskPriority;
  dependencies: Array<{ id?: string; step?: string; task_id?: string; [key: string]: unknown }>;
  plan_id: string | null;
  due_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  plan_source: string | null;
  source: TaskSource;
  source_metadata: Record<string, unknown> | null;
  last_tracked_at: string | null;
  created_at: string;
}

export interface TaskCreatePayload {
  title: string;
  description?: string | null;
  status?: TaskStatus;
  project?: string | null;
  assigned_agent?: string | null;
  priority?: TaskPriority;
  dependencies?: Task["dependencies"];
  plan_id?: string | null;
  due_at?: string | null;
  plan_source?: string | null;
  source?: TaskSource;
  source_metadata?: Record<string, unknown> | null;
}

export interface TaskUpdatePayload {
  title?: string;
  description?: string | null;
  status?: TaskStatus;
  project?: string | null;
  assigned_agent?: string | null;
  priority?: TaskPriority;
  dependencies?: Task["dependencies"];
  plan_id?: string | null;
  due_at?: string | null;
  plan_source?: string | null;
  source?: TaskSource;
  source_metadata?: Record<string, unknown> | null;
}

export interface TaskScanPlanItem {
  id: string;
  title: string;
  status: TaskStatus;
  priority: TaskPriority;
  due_at: string | null;
  assigned_agent: string | null;
  plan_id: string | null;
  source: TaskSource;
}

export interface TaskScanPlan {
  scanned_at: string;
  summary: {
    total: number;
    not_started: number;
    in_progress: number;
    done: number;
    overdue: number;
    upcoming: number;
    ready: number;
    blocked: number;
  };
  overdue: TaskScanPlanItem[];
  upcoming: TaskScanPlanItem[];
  blocked: TaskScanPlanItem[];
  ready: TaskScanPlanItem[];
  stalled: TaskScanPlanItem[];
  conflicts: TaskScanPlanItem[][];
  recommendations: string[];
}

export const TASKS_QUERY_KEY = ["tasks"] as const;

async function fetchTasks(project?: string): Promise<Task[]> {
  const url = new URL(`${API_BASE_URL}/tasks`);
  if (project) url.searchParams.set("project", project);
  const res = await authenticatedFetch(url.toString());
  if (!res.ok) throw new Error(`任务请求失败（${res.status}）`);
  return res.json() as Promise<Task[]>;
}

export function useTasksQuery(project?: string) {
  return useQuery({
    queryKey: project ? [...TASKS_QUERY_KEY, project] : TASKS_QUERY_KEY,
    queryFn: () => fetchTasks(project),
  });
}

async function fetchTask(taskId: string): Promise<Task | null> {
  const res = await authenticatedFetch(`${API_BASE_URL}/tasks/${taskId}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`任务请求失败（${res.status}）`);
  return res.json() as Promise<Task>;
}

/**
 * Detail deep links must not depend on the list cache: the task may sit outside
 * the current project filter, or the list may still be stale after a rename.
 */
export function useTaskQuery(taskId: string | null) {
  return useQuery({
    queryKey: [...TASKS_QUERY_KEY, "detail", taskId ?? ""],
    queryFn: () => fetchTask(taskId as string),
    enabled: Boolean(taskId),
  });
}

export const TASK_SCAN_PLAN_QUERY_KEY = ["tasks", "scan-plan"] as const;

export interface TaskScanPlanQuery {
  enabled?: boolean;
}

async function fetchTaskScanPlan(): Promise<TaskScanPlan> {
  const res = await authenticatedFetch(`${API_BASE_URL}/tasks/scan-plan`);
  if (!res.ok) throw new Error(`扫描任务规划失败（${res.status}）`);
  return res.json() as Promise<TaskScanPlan>;
}

export function useTaskScanPlanQuery(options: TaskScanPlanQuery = {}) {
  return useQuery({
    queryKey: TASK_SCAN_PLAN_QUERY_KEY,
    queryFn: fetchTaskScanPlan,
    enabled: options.enabled ?? true,
  });
}

function invalidateTasks(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: TASKS_QUERY_KEY });
  void queryClient.invalidateQueries({ queryKey: TASK_SCAN_PLAN_QUERY_KEY });
  // The calendar page embeds tasks (due within its window); keep it in sync when a
  // task is created/updated/deleted so "left deleted it, right still shows it" can't happen.
  void queryClient.invalidateQueries({ queryKey: ["calendar", "events"] });
}

/**
 * Task-list caches share `["tasks"]` / `["tasks", project]`. The scan plan has
 * a different shape, and the detail key (`["tasks","detail",id]`) holds a single
 * object rather than an array — both must be skipped.
 */
function isTaskListQueryKey(queryKey: readonly unknown[]): boolean {
  return (
    queryKey[0] === "tasks" &&
    queryKey.length <= 2 &&
    queryKey[1] !== "scan-plan" &&
    queryKey[1] !== "detail"
  );
}

interface TaskCacheSnapshot {
  queryKey: readonly unknown[];
  data: unknown;
}

/**
 * Patch every task list plus the calendar bundle immediately, before the PATCH /
 * DELETE round-trip. The sidebar "下一项" card and the kanban read these caches,
 * so ticking a task makes it disappear at once instead of after a refetch.
 */
async function patchTaskCaches(
  queryClient: ReturnType<typeof useQueryClient>,
  patch: (task: Task) => Task | null,
): Promise<TaskCacheSnapshot[]> {
  await queryClient.cancelQueries({ queryKey: TASKS_QUERY_KEY });
  const snapshots = queryClient.getQueriesData({ queryKey: TASKS_QUERY_KEY });

  queryClient.setQueriesData<Task[]>(
    { predicate: (query) => isTaskListQueryKey(query.queryKey) },
    (tasks) => {
      if (!Array.isArray(tasks)) return tasks;
      return tasks.flatMap((task) => {
        const patched = patch(task);
        return patched === null ? [] : [patched];
      });
    },
  );

  queryClient.setQueriesData<{ events: unknown[]; tasks: Task[] }>(
    { queryKey: ["calendar", "events"] },
    (bundle) => {
      if (!bundle || !Array.isArray(bundle.tasks)) return bundle;
      return {
        ...bundle,
        tasks: bundle.tasks.flatMap((task) => {
          const patched = patch(task);
          return patched === null ? [] : [patched];
        }),
      };
    },
  );

  return snapshots.map(([queryKey, data]) => ({ queryKey, data }));
}

function restoreTaskCaches(
  queryClient: ReturnType<typeof useQueryClient>,
  snapshots: TaskCacheSnapshot[] | undefined,
) {
  for (const snapshot of snapshots ?? []) {
    queryClient.setQueryData(snapshot.queryKey, snapshot.data);
  }
}

export function useCreateTaskMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: TaskCreatePayload) => {
      const res = await authenticatedFetch(`${API_BASE_URL}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `创建失败（${res.status}）`);
      }
      return res.json() as Promise<Task>;
    },
    onSettled: () => void invalidateTasks(queryClient),
  });
}

export function useUpdateTaskMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...payload }: TaskUpdatePayload & { id: string }) => {
      const res = await authenticatedFetch(`${API_BASE_URL}/tasks/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `更新失败（${res.status}）`);
      }
      return res.json() as Promise<Task>;
    },
    // 乐观更新：勾选完成后左侧"下一项日程"卡片必须立刻消失，不能等接口返回。
    onMutate: async ({ id, ...payload }) => {
      const snapshots = await patchTaskCaches(queryClient, (task) =>
        task.id === id ? ({ ...task, ...payload } as Task) : task,
      );
      return { snapshots };
    },
    onError: (_error, _variables, context) => {
      restoreTaskCaches(queryClient, context?.snapshots);
    },
    onSettled: () => void invalidateTasks(queryClient),
  });
}

export function useDeleteTaskMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (taskId: string) => {
      const res = await authenticatedFetch(`${API_BASE_URL}/tasks/${taskId}`, {
        method: "DELETE",
      });
      if (res.status === 404) throw new Error("任务不存在。");
      if (res.status !== 204) {
        const text = await res.text();
        throw new Error(text || `删除失败（${res.status}）`);
      }
    },
    onMutate: async (taskId: string) => {
      const snapshots = await patchTaskCaches(queryClient, (task) =>
        task.id === taskId ? null : task,
      );
      return { snapshots };
    },
    onError: (_error, _variables, context) => {
      restoreTaskCaches(queryClient, context?.snapshots);
    },
    onSettled: () => void invalidateTasks(queryClient),
  });
}
