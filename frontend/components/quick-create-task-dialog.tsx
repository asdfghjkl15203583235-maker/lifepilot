"use client";

import { useState } from "react";

import { useCreateTaskMutation } from "@/hooks/use-tasks";

/** 任务分类沿用看板的四个标准项目，避免出现无归属任务。 */
const PROJECT_OPTIONS = ["工作", "生活", "学习", "其他"] as const;

interface QuickCreateTaskDialogProps {
  onClose: () => void;
}

/**
 * 左侧「任务概览」的轻量新建入口：只收标题 / 截止时间 / 项目分类，
 * 直接调 POST /tasks。完整表单仍留在任务看板页。
 */
export function QuickCreateTaskDialog({ onClose }: QuickCreateTaskDialogProps) {
  const createTask = useCreateTaskMutation();
  const [title, setTitle] = useState("");
  const [dueAtLocal, setDueAtLocal] = useState("");
  const [project, setProject] = useState<string>(PROJECT_OPTIONS[0]);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = title.trim();
    if (!trimmed) {
      setError("请先填写任务标题。");
      return;
    }
    createTask.mutate(
      {
        title: trimmed,
        project,
        // datetime-local 是本地墙钟；转成绝对时刻再发，避免服务端按 UTC 解读。
        due_at: dueAtLocal ? new Date(dueAtLocal).toISOString() : null,
      },
      {
        onSuccess: () => onClose(),
        onError: (err) => setError(err instanceof Error ? err.message : "创建任务失败。"),
      },
    );
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      role="presentation"
      onClick={onClose}
    >
      <form
        onSubmit={handleSubmit}
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-sm overflow-hidden rounded-2xl border border-white/15 bg-zinc-950/95 shadow-2xl ring-1 ring-black/40"
        role="dialog"
        aria-modal="true"
        aria-label="快捷新建任务"
      >
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <h2 className="text-[14px] font-semibold text-white">新建任务</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded-lg p-1 text-white/45 transition hover:bg-white/10 hover:text-white"
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium text-white/55">标题</span>
            <input
              autoFocus
              type="text"
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：周五前提交周报"
              maxLength={500}
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white placeholder-white/30 outline-none transition focus:border-violet-400/50 focus:bg-white/8"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium text-white/55">截止时间（可选）</span>
            <input
              type="datetime-local"
              value={dueAtLocal}
              onChange={(event) => setDueAtLocal(event.target.value)}
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white outline-none transition focus:border-violet-400/50 focus:bg-white/8"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] font-medium text-white/55">项目分类</span>
            <select
              value={project}
              onChange={(event) => setProject(event.target.value)}
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-[14px] text-white outline-none transition focus:border-violet-400/50 focus:bg-white/8"
            >
              {PROJECT_OPTIONS.map((option) => (
                <option key={option} value={option} className="bg-zinc-900 text-white">
                  {option}
                </option>
              ))}
            </select>
          </label>

          {error ? <p className="text-[12px] text-rose-300">{error}</p> : null}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-white/10 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-white/15 bg-white/5 px-3 py-1.5 text-[13px] text-white/75 transition hover:bg-white/10"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={createTask.isPending}
            className="rounded-lg bg-violet-600 px-3 py-1.5 text-[13px] font-medium text-white transition hover:bg-violet-500 disabled:opacity-50"
          >
            {createTask.isPending ? "创建中…" : "创建"}
          </button>
        </div>
      </form>
    </div>
  );
}
