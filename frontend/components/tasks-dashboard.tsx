"use client";

import { useSearchParams } from "next/navigation";
import { memo, useCallback, useEffect, useMemo, useState } from "react";

import {
  AGENT_OPTIONS,
  type Task,
  type TaskStatus,
  useCreateTaskMutation,
  useDeleteTaskMutation,
  useTaskQuery,
  useTasksQuery,
  useUpdateTaskMutation,
} from "@/hooks/use-tasks";

// ─── Icons ────────────────────────────────────────────────────────────────────

function IconPlus({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function IconClose({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M18 6L6 18M6 6l12 12" />
    </svg>
  );
}

function IconTrash({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
      />
    </svg>
  );
}

function IconEdit({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
      />
    </svg>
  );
}

function IconRobot({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 3H7a2 2 0 00-2 2v1m0 0H3m2 0v10a2 2 0 002 2h10a2 2 0 002-2V6m0 0h2m-2 0V5a2 2 0 00-2-2h-2M9 3h6M9 3V1m6 2V1M9 12h.01M15 12h.01"
      />
    </svg>
  );
}

function IconCheck({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
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

function IconClock({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function countdownText(dueAt: string | null): string | null {
  if (!dueAt) return null;
  const due = new Date(dueAt);
  if (Number.isNaN(due.getTime())) return null;
  const diffMs = due.getTime() - Date.now();
  if (diffMs <= 0) return null;
  const mins = Math.round(diffMs / 60_000);
  if (mins < 1) return null;
  if (mins < 60) return `${mins} 分钟后`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) {
    const rem = mins % 60;
    return rem ? `${hours} 小时${rem} 分后` : `${hours} 小时后`;
  }
  const days = Math.floor(hours / 24);
  return `${days} 天后`;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const STATUS_META: Record<TaskStatus, { label: string; pill: string; column: string }> = {
  not_started: {
    label: "未开始",
    pill: "bg-zinc-500/20 text-zinc-300 ring-1 ring-zinc-500/25",
    column: "border-zinc-500/20",
  },
  // 看板不再有"进行中"列：执行过程由代理自动推进，用户只需看"还没做"和
  // "做完了"。状态机里仍保留 in_progress，它会被归并到"未开始"列显示。
  in_progress: {
    label: "未开始",
    pill: "bg-zinc-500/20 text-zinc-300 ring-1 ring-zinc-500/25",
    column: "border-zinc-500/20",
  },
  done: {
    label: "已完成",
    pill: "bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-500/25",
    column: "border-emerald-500/20",
  },
};

const AGENT_LABEL: Record<string, string> = Object.fromEntries(
  AGENT_OPTIONS.map((a) => [a.value, a.label]),
);

type BoardColumnStatus = "not_started" | "done";

const STATUS_ORDER: BoardColumnStatus[] = ["not_started", "done"];

// ─── Task form modal ───────────────────────────────────────────────────────────

interface TaskFormValues {
  title: string;
  description: string;
  status: TaskStatus;
  project: string;
  /** datetime-local 值：本地墙钟 `YYYY-MM-DDTHH:mm`，空串表示不设截止时间。 */
  dueAtLocal: string;
}

const EMPTY_FORM: TaskFormValues = {
  title: "",
  description: "",
  status: "not_started",
  project: "",
  dueAtLocal: "",
};

/** ISO 字符串 → 本地 datetime-local（编辑回填用）；空值/非法值返回空串。 */
function toLocalDatetimeLocal(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  // toLocaleString 在某些浏览器会丢秒/零填充不一致，手拼最稳。
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 本地 datetime-local → ISO UTC 字符串（提交用）；空串返回 null。 */
function toIsoFromLocal(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

interface TaskFormModalProps {
  initial?: Partial<TaskFormValues>;
  onSave: (values: TaskFormValues) => void;
  onClose: () => void;
  isSaving: boolean;
  mode: "create" | "edit";
  allProjects: string[];
}

function TaskFormModal({ initial, onSave, onClose, isSaving, mode, allProjects }: TaskFormModalProps) {
  const [form, setForm] = useState<TaskFormValues>({ ...EMPTY_FORM, ...initial });

  const set = <K extends keyof TaskFormValues>(key: K, value: TaskFormValues[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.title.trim()) return;
    onSave(form);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" aria-hidden onClick={onClose} />
      <div className="relative z-10 w-full max-w-lg overflow-hidden rounded-2xl border border-white/15 bg-zinc-950/95 shadow-2xl ring-1 ring-black/40">
        <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
          <h2 className="text-[15px] font-semibold text-white">
            {mode === "create" ? "新建任务" : "编辑任务"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-white/45 transition hover:bg-white/10 hover:text-white"
            aria-label="关闭"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4 p-5">
          <div className="flex flex-col gap-1.5">
            <label className="text-[12px] font-medium text-white/55">标题 *</label>
            <input
              type="text"
              value={form.title}
              onChange={(e) => set("title", e.target.value)}
              placeholder="需要完成什么？"
              required
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white placeholder-white/30 outline-none ring-0 transition focus:border-violet-400/50 focus:bg-white/8"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <label className="text-[12px] font-medium text-white/55">描述</label>
            <textarea
              value={form.description}
              onChange={(e) => set("description", e.target.value)}
              placeholder="补充细节…"
              rows={3}
              className="resize-none rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <label className="text-[12px] font-medium text-white/55">状态</label>
              <select
                value={form.status === "in_progress" ? "not_started" : form.status}
                onChange={(e) => set("status", e.target.value as TaskStatus)}
                className="rounded-lg border border-white/15 bg-zinc-900 px-3 py-2 text-[14px] text-white outline-none transition focus:border-violet-400/50"
              >
                <option value="not_started">未开始</option>
                <option value="done">已完成</option>
              </select>
            </div>

            <div className="flex flex-col gap-1.5">
              <label className="text-[12px] font-medium text-white/55">项目</label>
              <input
                type="text"
                value={form.project}
                onChange={(e) => set("project", e.target.value)}
                placeholder="例如：Q2 发布"
                list="project-suggestions"
                className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
              />
              <datalist id="project-suggestions">
                {allProjects.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <label className="text-[12px] font-medium text-white/55">截止时间</label>
              {form.dueAtLocal ? (
                <button
                  type="button"
                  onClick={() => set("dueAtLocal", "")}
                  className="text-[11px] text-white/40 transition hover:text-white/80"
                >
                  清除
                </button>
              ) : null}
            </div>
            <input
              type="datetime-local"
              value={form.dueAtLocal}
              onChange={(e) => set("dueAtLocal", e.target.value)}
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8 [color-scheme:dark]"
            />
            <p className="text-[11px] text-white/30">留空表示无截止时间（随时可做）。</p>
          </div>

          <div className="flex justify-end gap-2 border-t border-white/10 pt-3">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg px-4 py-2 text-[13px] font-medium text-white/60 transition hover:bg-white/8 hover:text-white"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={isSaving || !form.title.trim()}
              className="rounded-lg bg-violet-600 px-4 py-2 text-[13px] font-medium text-white shadow transition hover:bg-violet-500 disabled:opacity-50"
            >
              {isSaving ? "正在保存…" : mode === "create" ? "创建任务" : "保存修改"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ─── Task detail drawer ────────────────────────────────────────────────────────

interface TaskDetailDrawerProps {
  task: Task;
  onClose: () => void;
  onMarkDone: (taskId: string) => void;
  isUpdating: boolean;
}

function TaskDetailDrawer({ task, onClose, onMarkDone, isUpdating }: TaskDetailDrawerProps) {
  const meta = STATUS_META[task.status];
  const countdown = countdownText(task.due_at);

  const dependencyLines = (task.dependencies ?? [])
    .map((dep) => {
      const id = typeof dep === "string" ? dep : dep?.id ?? dep?.step ?? dep?.task_id;
      return id ? String(id) : null;
    })
    .filter((id): id is string => Boolean(id));

  return (
    <div
      className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/60 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <aside className="flex h-full w-full max-w-md flex-col overflow-hidden border-l border-white/15 bg-zinc-950/95 shadow-2xl ring-1 ring-black/40">
        <div className="flex items-center justify-between border-b border-white/10 px-5 py-4">
          <h2 className="text-[15px] font-semibold text-white">任务详情</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-white/45 transition hover:bg-white/10 hover:text-white"
            aria-label="关闭"
          >
            <IconClose className="h-4 w-4" />
          </button>
        </div>

        <div className="flex flex-1 flex-col gap-4 overflow-y-auto p-5">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${meta.pill}`}>{meta.label}</span>
            {task.source === "system" ? (
              <span className="rounded-full bg-violet-500/15 px-2.5 py-0.5 text-[11px] font-medium text-violet-200/80 ring-1 ring-violet-500/20">
                系统
              </span>
            ) : null}
          </div>

          <div>
            <h3 className="text-lg font-semibold leading-snug text-white">{task.title}</h3>
            {countdown ? (
              <p className="mt-1 flex items-center gap-1 text-[12px] text-amber-200/80">
                <IconClock className="h-3.5 w-3.5" />
                {countdown}
              </p>
            ) : null}
          </div>

          {task.description ? (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-white/45">描述</p>
              <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-white/75">{task.description}</p>
            </div>
          ) : null}

          <dl className="grid grid-cols-2 gap-3">
            <div>
              <dt className="text-[11px] font-semibold uppercase tracking-wide text-white/45">优先级</dt>
              <dd className="mt-1 text-[13px] capitalize text-white/85">{task.priority}</dd>
            </div>
            {task.project ? (
              <div>
                <dt className="text-[11px] font-semibold uppercase tracking-wide text-white/45">项目</dt>
                <dd className="mt-1 text-[13px] text-white/85">{task.project}</dd>
              </div>
            ) : null}
            {task.assigned_agent ? (
              <div>
                <dt className="text-[11px] font-semibold uppercase tracking-wide text-white/45">指派代理</dt>
                <dd className="mt-1 flex items-center gap-1 text-[13px] text-white/85">
                  <IconRobot className="h-3.5 w-3.5" />
                  {AGENT_LABEL[task.assigned_agent] ?? task.assigned_agent}
                </dd>
              </div>
            ) : null}
            {task.due_at ? (
              <div>
                <dt className="text-[11px] font-semibold uppercase tracking-wide text-white/45">截止时间</dt>
                <dd className="mt-1 text-[13px] text-white/85">
                  {new Date(task.due_at).toLocaleString("zh-CN", { hour12: false })}
                </dd>
              </div>
            ) : null}
          </dl>

          {dependencyLines.length > 0 ? (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-white/45">依赖</p>
              <div className="flex flex-wrap gap-1.5">
                {dependencyLines.map((id) => (
                  <span key={id} className="rounded-md bg-white/8 px-1.5 py-0.5 text-[11px] font-mono text-white/60">
                    {id}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          {task.source_metadata ? (
            <div>
              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-white/45">来源信息</p>
              <pre className="rounded-lg bg-white/4 p-2 text-[11px] leading-relaxed text-white/55 overflow-x-auto">
                {JSON.stringify(task.source_metadata, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>

        <div className="border-t border-white/10 px-5 py-4">
          <button
            type="button"
            onClick={() => onMarkDone(task.id)}
            disabled={isUpdating || task.status === "done"}
            className="w-full rounded-lg bg-emerald-600 px-4 py-2.5 text-[13px] font-medium text-white shadow transition hover:bg-emerald-500 disabled:opacity-50"
          >
            {task.status === "done" ? "已完成 ✓" : isUpdating ? "正在标记…" : "标为已完成"}
          </button>
        </div>
      </aside>
    </div>
  );
}

// ─── Task card ─────────────────────────────────────────────────────────────────

interface TaskCardProps {
  task: Task;
  onEdit: (task: Task) => void;
  onDelete: (taskId: string) => void;
  onStatusChange: (taskId: string, status: TaskStatus) => void;
  onOpen: (task: Task) => void;
  isDeleting: boolean;
  isMenuOpen: boolean;
  onMenuToggle: (taskId: string | null) => void;
}

/**
 * `memo` + a menu open flag passed from the parent: task cards are re-rendered on
 * every keystroke/scroll otherwise, and the dropdown state must survive that.
 */
const TaskCard = memo(function TaskCard({
  task,
  onEdit,
  onDelete,
  onStatusChange,
  onOpen,
  isDeleting,
  isMenuOpen,
  onMenuToggle,
}: TaskCardProps) {
  const meta = STATUS_META[task.status];

  // 两列看板：未开始 ⇄ 已完成（in_progress 的行也归在未开始列）。
  const nextStatus: Record<TaskStatus, TaskStatus> = {
    not_started: "done",
    in_progress: "done",
    done: "not_started",
  };

  const countdown = countdownText(task.due_at);
  const isDone = task.status === "done";

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(task)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(task);
        }
      }}
      className={`group relative flex cursor-pointer flex-col gap-2.5 rounded-xl border bg-black/40 p-3.5 shadow-sm transition hover:bg-black/55 ${meta.column}`}
    >
      <div className="flex items-start gap-2">
        <span
          role="checkbox"
          aria-checked={isDone}
          aria-label={isDone ? "任务已完成" : "标为已完成"}
          tabIndex={0}
          onClick={(event) => {
            event.stopPropagation();
            if (!isDone) onStatusChange(task.id, "done");
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              event.stopPropagation();
              if (!isDone) onStatusChange(task.id, "done");
            }
          }}
          className={`mt-0.5 flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded border transition ${
            isDone
              ? "border-emerald-400/50 bg-emerald-400/20 text-emerald-200"
              : "border-white/30 bg-transparent text-transparent hover:border-emerald-400/60 hover:text-emerald-300/50"
          }`}
        >
          <IconCheck className="h-3 w-3" />
        </span>

        <div className="min-w-0 flex-1">
          <p className={`text-[13px] font-medium leading-snug ${isDone ? "text-white/45 line-through" : "text-white/90"}`}>
            {task.title}
          </p>
          {countdown ? (
            <p className="mt-0.5 flex items-center gap-1 text-[11px] text-amber-200/70">
              <IconClock className="h-3 w-3" />
              {countdown}
            </p>
          ) : null}
        </div>

        <div className="relative shrink-0" data-task-menu>
          <button
            type="button"
            aria-label="任务操作"
            title="任务操作"
            aria-haspopup="menu"
            aria-expanded={isMenuOpen}
            onClick={(e) => {
              e.stopPropagation();
              onMenuToggle(isMenuOpen ? null : task.id);
            }}
            className="rounded-md p-1 text-white/45 transition hover:bg-white/10 hover:text-white/90"
          >
            <IconDotsHorizontal className="h-4 w-4" />
          </button>
          {isMenuOpen ? (
            <div
              role="menu"
              onClick={(e) => e.stopPropagation()}
              className="absolute right-0 top-7 z-30 w-32 overflow-hidden rounded-lg border border-white/15 bg-zinc-900/95 py-1 shadow-xl backdrop-blur-md"
            >
              <button
                type="button"
                role="menuitem"
                onClick={(e) => {
                  e.stopPropagation();
                  onMenuToggle(null);
                  onEdit(task);
                }}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-white/80 transition hover:bg-white/10 hover:text-white"
              >
                <IconEdit className="h-3.5 w-3.5" />
                编辑
              </button>
              <button
                type="button"
                role="menuitem"
                disabled={isDeleting}
                onClick={(e) => {
                  e.stopPropagation();
                  onMenuToggle(null);
                  onDelete(task.id);
                }}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px] font-medium text-rose-300 transition hover:bg-rose-500/15 hover:text-rose-100 disabled:opacity-40"
              >
                <IconTrash className="h-3.5 w-3.5" />
                删除
              </button>
            </div>
          ) : null}
        </div>
      </div>

      {task.description ? (
        <p className="line-clamp-2 text-[12px] leading-relaxed text-white/50">{task.description}</p>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        {task.project ? (
          <span className="rounded-md bg-violet-500/15 px-1.5 py-0.5 text-[11px] font-medium text-violet-200/80 ring-1 ring-violet-500/20">
            {task.project}
          </span>
        ) : null}
        {task.assigned_agent ? (
          <span className="flex items-center gap-1 rounded-md bg-sky-500/15 px-1.5 py-0.5 text-[11px] font-medium text-sky-200/80 ring-1 ring-sky-500/20">
            <IconRobot className="h-3 w-3" />
            {AGENT_LABEL[task.assigned_agent] ?? task.assigned_agent}
          </span>
        ) : null}
      </div>

      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onStatusChange(task.id, nextStatus[task.status]);
        }}
        className={`mt-0.5 self-start rounded-full px-2 py-0.5 text-[11px] font-medium transition hover:scale-105 ${meta.pill}`}
      >
        {meta.label}
      </button>
    </div>
  );
});

// ─── Column ────────────────────────────────────────────────────────────────────

interface ColumnProps {
  status: BoardColumnStatus;
  tasks: Task[];
  onEdit: (task: Task) => void;
  onDelete: (taskId: string) => void;
  onStatusChange: (taskId: string, status: TaskStatus) => void;
  onOpen: (task: Task) => void;
  deletingId: string | null;
  menuOpenTaskId: string | null;
  onMenuToggle: (taskId: string | null) => void;
}

function Column({
  status,
  tasks,
  onEdit,
  onDelete,
  onStatusChange,
  onOpen,
  deletingId,
  menuOpenTaskId,
  onMenuToggle,
}: ColumnProps) {
  const meta = STATUS_META[status];
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-white/50">{meta.label}</h3>
        <span className="rounded-full bg-white/8 px-2 py-0.5 text-[11px] font-medium tabular-nums text-white/45">
          {tasks.length}
        </span>
      </div>
      <div className="flex flex-col gap-2 overflow-y-auto pb-1">
        {tasks.length === 0 ? (
          <div className="rounded-xl border border-dashed border-white/10 py-6 text-center text-[12px] text-white/25">
            暂无任务
          </div>
        ) : (
          tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              onEdit={onEdit}
              onDelete={onDelete}
              onStatusChange={onStatusChange}
              onOpen={onOpen}
              isDeleting={deletingId === task.id}
              isMenuOpen={menuOpenTaskId === task.id}
              onMenuToggle={onMenuToggle}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ─── Main dashboard ────────────────────────────────────────────────────────────

export function TasksDashboard() {
  const [projectFilter, setProjectFilter] = useState<string>("");
  const [editingTask, setEditingTask] = useState<Task | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  /** 当前展开"三点"菜单的任务 id；同一时刻只允许一个菜单。 */
  const [menuOpenTaskId, setMenuOpenTaskId] = useState<string | null>(null);

  const { data: tasks = [], isError, error, isPending, refetch } = useTasksQuery();
  const createTask = useCreateTaskMutation();
  const updateTask = useUpdateTaskMutation();
  const deleteTask = useDeleteTaskMutation();

  // 只渲染用户主动待办（source=user），过滤系统内部任务。
  const userTasks = tasks.filter((task) => task.source !== "system");

  // 约定排序：标准四分类在前（工作/生活/学习/其他），其余项目按字母序排后。
  const PROJECT_ORDER = ["工作", "生活", "学习", "其他"];
  const projects = userTasks
    .map((t) => t.project)
    .filter((p): p is string => typeof p === "string");
  const otherProjects = Array.from(new Set(projects.filter((p) => !PROJECT_ORDER.includes(p)))).sort();
  const allProjects = [
    ...PROJECT_ORDER.filter((p) => projects.includes(p)),
    ...otherProjects,
  ];

  const displayedTasks = projectFilter
    ? userTasks.filter((t) => t.project === projectFilter)
    : userTasks;

  // in_progress 归并进"未开始"列：看板只保留两列。
  const tasksByStatus: Record<"not_started" | "done", Task[]> = {
    not_started: displayedTasks.filter(
      (t) => t.status === "not_started" || t.status === "in_progress",
    ),
    done: displayedTasks.filter((t) => t.status === "done"),
  };

  // 支持 /tasks?id=xxx 直达详情抽屉。用 useSearchParams 而不是只读一次
  // window.location：从侧栏"查看任务"跳到当前页时组件不会重新挂载，只读一次
  // 会导致抽屉打不开。
  const searchParams = useSearchParams();
  const taskIdFromUrl = searchParams.get("id");
  useEffect(() => {
    if (taskIdFromUrl) {
      setOpenTaskId(taskIdFromUrl);
    }
  }, [taskIdFromUrl]);

  const taskFromList = useMemo(
    () => (openTaskId ? tasks.find((task) => task.id === openTaskId) ?? null : null),
    [tasks, openTaskId],
  );
  // 列表尚未加载完，或任务被项目筛选/缓存遗漏时，按 id 单独拉一次；
  // 只依赖列表的话深链会是一片空白，用户看到的就是"任务详情 404"。
  const { data: fetchedTask, isSuccess: taskFetchSettled } = useTaskQuery(
    taskFromList ? null : openTaskId,
  );
  const openTask = taskFromList ?? fetchedTask ?? null;

  // 链接里的任务确实不存在（已删除/换账号）时清掉参数，别停在空详情。
  useEffect(() => {
    if (!openTaskId || taskFromList || !taskFetchSettled) {
      return;
    }
    if (fetchedTask === null) {
      setOpenTaskId(null);
      window.history.replaceState(null, "", window.location.pathname);
    }
  }, [openTaskId, taskFromList, taskFetchSettled, fetchedTask]);

  const closeDrawer = useCallback(() => {
    setOpenTaskId(null);
    window.history.replaceState(null, "", window.location.pathname);
  }, []);

  const handleOpen = useCallback((task: Task) => {
    setOpenTaskId(task.id);
    window.history.replaceState(null, "", `${window.location.pathname}?id=${task.id}`);
  }, []);

  const handleCreate = (values: TaskFormValues) => {
    createTask.mutate(
      {
        title: values.title,
        description: values.description || null,
        status: values.status,
        project: values.project || null,
        due_at: toIsoFromLocal(values.dueAtLocal),
      },
      { onSuccess: () => setIsCreateOpen(false) },
    );
  };

  const handleEdit = (values: TaskFormValues) => {
    if (!editingTask) return;
    updateTask.mutate(
      {
        id: editingTask.id,
        title: values.title,
        description: values.description || null,
        status: values.status,
        project: values.project || null,
        due_at: toIsoFromLocal(values.dueAtLocal),
      },
      { onSuccess: () => setEditingTask(null) },
    );
  };

  const handleStatusChange = (taskId: string, status: TaskStatus) => {
    updateTask.mutate({ id: taskId, status });
  };

  const handleDelete = (taskId: string) => {
    if (!window.confirm("删除这个任务？删除后无法恢复。")) return;
    deleteTask.mutate(taskId);
  };

  const handleMenuToggle = useCallback((taskId: string | null) => {
    setMenuOpenTaskId(taskId);
  }, []);

  // 点击空白处 / 按 Esc 收起菜单：菜单挂在卡片内部，外层滚动容器会裁剪
  // 定位，所以用文档级监听而不是 CSS :focus-within。
  useEffect(() => {
    if (menuOpenTaskId === null) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!(event.target instanceof Element)) return;
      if (!event.target.closest("[data-task-menu]")) {
        setMenuOpenTaskId(null);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpenTaskId(null);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpenTaskId]);

  const errorMessage = isError && error instanceof Error ? error.message : isError ? "任务加载失败" : null;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden pt-1">
      {/* Toolbar */}
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setIsCreateOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-[13px] font-medium text-white shadow transition hover:bg-violet-500"
        >
          <IconPlus className="h-4 w-4" />
          新建任务
        </button>

        {allProjects.length > 0 ? (
          <div className="flex items-center gap-1.5">
            <span className="text-[12px] text-white/40">项目：</span>
            <button
              type="button"
              onClick={() => setProjectFilter("")}
              className={`rounded-full px-2.5 py-1 text-[12px] font-medium transition ${
                !projectFilter
                  ? "bg-white/15 text-white"
                  : "text-white/50 hover:bg-white/8 hover:text-white/80"
              }`}
            >
              全部
            </button>
            {allProjects.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setProjectFilter(p === projectFilter ? "" : p)}
                className={`rounded-full px-2.5 py-1 text-[12px] font-medium transition ${
                  projectFilter === p
                    ? "bg-violet-500/30 text-violet-200 ring-1 ring-violet-400/30"
                    : "text-white/50 hover:bg-white/8 hover:text-white/80"
                }`}
              >
                {p}
              </button>
            ))}
          </div>
        ) : null}

        {errorMessage ? (
          <p className="text-[12px] text-rose-300">
            {errorMessage}{" "}
            <button
              type="button"
              className="underline"
              onClick={() => void refetch()}
            >
              重试
            </button>
          </p>
        ) : null}

        {isPending ? (
          <span className="text-[12px] animate-pulse text-white/40">正在加载…</span>
        ) : null}
      </div>

      {/* Kanban board */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-hidden sm:grid-cols-2">
        {STATUS_ORDER.map((s) => (
          <Column
            key={s}
            status={s}
            tasks={tasksByStatus[s]}
            onEdit={setEditingTask}
            onDelete={handleDelete}
            onStatusChange={handleStatusChange}
            onOpen={handleOpen}
            deletingId={deleteTask.isPending && typeof deleteTask.variables === "string" ? deleteTask.variables : null}
            menuOpenTaskId={menuOpenTaskId}
            onMenuToggle={handleMenuToggle}
          />
        ))}
      </div>

      {/* Create modal */}
      {isCreateOpen ? (
        <TaskFormModal
          mode="create"
          allProjects={allProjects}
          isSaving={createTask.isPending}
          onSave={handleCreate}
          onClose={() => setIsCreateOpen(false)}
        />
      ) : null}

      {/* Edit modal */}
      {editingTask ? (
        <TaskFormModal
          mode="edit"
          initial={{
            title: editingTask.title,
            description: editingTask.description ?? "",
            status: editingTask.status,
            project: editingTask.project ?? "",
            dueAtLocal: toLocalDatetimeLocal(editingTask.due_at),
          }}
          allProjects={allProjects}
          isSaving={updateTask.isPending}
          onSave={handleEdit}
          onClose={() => setEditingTask(null)}
        />
      ) : null}

      {/* Detail drawer (/?id=xxx) */}
      {openTask ? (
        <TaskDetailDrawer
          task={openTask}
          onClose={closeDrawer}
          onMarkDone={handleStatusChange.bind(null, openTask.id, "done")}
          isUpdating={updateTask.isPending}
        />
      ) : null}
    </div>
  );
}
