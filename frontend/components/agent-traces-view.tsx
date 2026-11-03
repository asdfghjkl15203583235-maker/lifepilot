"use client";

import { format } from "date-fns";
import { useMemo, useState } from "react";

import { RunTraceModal } from "./run-trace-modal";
import { useBackgroundJobsQuery } from "@/hooks/use-background-jobs";
import {
  RUN_OUTCOME_LABEL,
  type JobRun,
  type RunOutcome,
} from "@/lib/mock-background-jobs";

const EMPTY_STATE_MESSAGE = "还没有真实的代理运行记录。定时任务每执行一次，就会在这里留下一条运行轨迹。";

const OUTCOME_PILL: Record<RunOutcome, string> = {
  success: "bg-emerald-500/20 text-emerald-200/95",
  failed: "bg-red-500/20 text-red-200/95",
  skipped: "bg-amber-500/20 text-amber-200/95",
};

const OUTCOME_DOT: Record<RunOutcome, string> = {
  success: "bg-emerald-400",
  failed: "bg-red-400",
  skipped: "bg-amber-400",
};

function IconChevronLeft({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 18l-6-6 6-6" />
    </svg>
  );
}

function IconClockSmall({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function formatRunTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return format(d, "MM月dd日 HH:mm:ss");
}

function formatDuration(ms: number): string {
  if (!ms || ms <= 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m} 分 ${rem} 秒` : `${m} 分钟`;
}

export interface AgentTracesViewProps {
  onExit: () => void;
}

export function AgentTracesView({ onExit }: AgentTracesViewProps) {
  const { data, isPending, isError, error } = useBackgroundJobsQuery();
  const [selectedRun, setSelectedRun] = useState<JobRun | null>(null);

  const runs = useMemo(() => (data?.runs ?? []).slice(), [data]);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-white/10  shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]">
      <header className="mb-4 flex shrink-0 items-center justify-between gap-3 border-b border-white/10 px-3 py-3 sm:px-4">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-white/45">代理追踪</p>
          <h2 className="truncate text-lg font-semibold text-white/95">运行与工作流</h2>
        </div>
        <button
          type="button"
          onClick={onExit}
          className="flex shrink-0 items-center gap-1.5 rounded-full border border-white/18 bg-black/35 px-3 py-1.5 text-sm font-medium text-white/92 transition hover:border-white/28 hover:bg-black/45"
        >
          <IconChevronLeft className="h-4 w-4" aria-hidden />
          返回对话
        </button>
      </header>

      <div className="flex min-h-0 flex-1 flex-col overflow-hidden px-2 pb-4">
        <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overscroll-contain">
          {isPending ? (
            <div className="flex flex-1 flex-col items-center justify-center rounded-xl border border-dashed border-white/12 bg-black/45 px-6 py-12 text-center">
              <p className="animate-pulse text-sm text-white/45">正在加载运行记录…</p>
            </div>
          ) : isError ? (
            <div className="flex flex-1 flex-col items-center justify-center rounded-xl border border-dashed border-white/12 bg-black/45 px-6 py-12 text-center">
              <p className="max-w-sm text-sm leading-relaxed text-rose-300">
                {error instanceof Error ? error.message : "无法加载运行记录。"}
              </p>
            </div>
          ) : runs.length === 0 ? (
            <div className="flex flex-1 flex-col items-center justify-center rounded-xl border border-dashed border-white/12 bg-black/45 px-6 py-12 text-center">
              <p className="max-w-sm text-sm leading-relaxed text-white/58">{EMPTY_STATE_MESSAGE}</p>
            </div>
          ) : (
            runs.map((run) => (
              <button
                key={run.id}
                type="button"
                onClick={() => setSelectedRun(run)}
                className="w-full rounded-xl border border-white/10 bg-black/35 px-3 py-3 text-left transition hover:border-white/18 hover:bg-black/48"
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="line-clamp-2 text-sm font-medium leading-snug text-white/92">{run.jobTitle}</p>
                  <span className={`shrink-0 rounded-md px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide ${OUTCOME_PILL[run.outcome]}`}>
                    {RUN_OUTCOME_LABEL[run.outcome]}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-[13px] leading-snug text-white/58">{run.detail}</p>
                <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-white/40">
                  <span className="inline-flex items-center gap-1.5">
                    <span className={`h-1.5 w-1.5 rounded-full ${OUTCOME_DOT[run.outcome]}`} />
                    {run.traces.length} 步
                  </span>
                  <span className="inline-flex items-center gap-1">
                    <IconClockSmall className="h-3.5 w-3.5 opacity-70" aria-hidden />
                    {formatRunTime(run.startedAtIso)}
                  </span>
                  <span>耗时 {formatDuration(run.durationMs)}</span>
                </div>
              </button>
            ))
          )}
        </div>
      </div>

      <RunTraceModal run={selectedRun} onClose={() => setSelectedRun(null)} />
    </div>
  );
}
