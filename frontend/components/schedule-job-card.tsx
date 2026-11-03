"use client";

import { format } from "date-fns";

import {
  ACTION_TYPE_LABEL,
  JOB_STATUS_ACTIVE,
  JOB_STATUS_ENDED,
  JOB_STATUS_LABEL,
  JOB_STATUS_PAUSED,
  type BackgroundJob,
  type JobStatus,
} from "@/lib/mock-background-jobs";

const STATUS_BADGE_CLASS: Record<JobStatus, string> = {
  [JOB_STATUS_ACTIVE]: "border-emerald-400/35 bg-emerald-500/15 text-emerald-100",
  [JOB_STATUS_PAUSED]: "border-amber-400/35 bg-amber-500/15 text-amber-100",
  [JOB_STATUS_ENDED]: "border-white/15 bg-white/10 text-white/70",
};

export interface ScheduleJobCardProps {
  readonly job: BackgroundJob;
  readonly density?: "comfortable" | "compact";
  /** Softer borders and lifts contrast when nested inside tinted modal panels */
  readonly surface?: "default" | "modal";
  readonly canManage?: boolean;
  readonly onStopJob?: (jobId: string) => void;
  readonly onDeleteJob?: (jobId: string) => void;
  readonly isStopPending?: boolean;
  readonly isDeletePending?: boolean;
}

const DELETE_CONFIRM_TEXT = "永久删除这个定时任务？此操作无法撤销。";

export function ScheduleJobCard({
  job,
  density = "comfortable",
  surface = "default",
  canManage = false,
  onStopJob,
  onDeleteJob,
  isStopPending = false,
  isDeletePending = false,
}: ScheduleJobCardProps) {
  const isCompact = density === "compact";
  const paddingClass = isCompact ? "p-3" : "p-4";
  const titleClass = isCompact ? "text-sm" : "text-[15px]";

  const whenLine = (() => {
    if (job.status === JOB_STATUS_ENDED && job.endedAtIso) {
      return `已结束 ${format(new Date(job.endedAtIso), "yyyy年M月d日")}`;
    }
    if (job.nextRunIso) {
      return format(new Date(job.nextRunIso), "yyyy年M月d日 · HH:mm");
    }
    return "—";
  })();

  const surfaceClass =
    surface === "modal"
      ? "border-white/[0.09] bg-zinc-900/55 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)] ring-1 ring-white/[0.04]"
      : "border-white/[0.10] bg-black/35 shadow-[inset_0_1px_0_rgba(255,255,255,0.06)]";

  const showStop = canManage && job.status === JOB_STATUS_ACTIVE && onStopJob;
  const showDelete = canManage && onDeleteJob;
  const actionBtnBase =
    "rounded-lg border px-2.5 py-1.5 text-[12px] font-medium transition enabled:hover:border-white/30 enabled:hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50";
  const stopBtnClass =
    surface === "modal"
      ? "border-amber-400/25 text-amber-100/95"
      : "border-amber-400/20 text-amber-100/90";
  const deleteBtnClass = "border-rose-400/20 text-rose-100/90";

  return (
    <article className={`flex flex-col rounded-xl border ${surfaceClass} ${paddingClass}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className={`font-semibold leading-snug text-white ${titleClass}`}>{job.title}</h3>
        <span
          className={`shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${STATUS_BADGE_CLASS[job.status]}`}
        >
          {JOB_STATUS_LABEL[job.status]}
        </span>
      </div>

      <p className="mt-2 line-clamp-2 text-[12px] leading-snug text-white/55" title={job.instruction}>
        {job.instruction}
      </p>

      <div className="mt-3 flex flex-wrap gap-1.5">
        {job.actions.map((action, actionIndex) => (
          <span
            key={`${job.id}-action-${actionIndex}`}
            className={`rounded-md border px-2 py-0.5 text-[11px] font-medium ${
              surface === "modal"
                ? "border-violet-400/15 bg-violet-500/[0.09] text-white/85"
                : "border-white/10 bg-white/6 text-white/80"
            }`}
          >
            <span className="text-white/45">{ACTION_TYPE_LABEL[action.actionType]}:</span>{" "}
            <span className="text-white/85">{action.label}</span>
          </span>
        ))}
      </div>

      <dl className={`mt-3 grid gap-2 text-[12px] text-white/65 ${isCompact ? "" : "sm:grid-cols-2"}`}>
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-white/38">计划</dt>
          <dd className="mt-0.5 text-white/78">{job.scheduleDescription}</dd>
        </div>
        <div>
          <dt className="text-[11px] uppercase tracking-wide text-white/38">
            {job.status === JOB_STATUS_ENDED ? "已结束" : "下次运行"}
          </dt>
          <dd className="mt-0.5 tabular-nums text-white/78">{whenLine}</dd>
        </div>
        <div className={isCompact ? "sm:col-span-2" : "sm:col-span-2"}>
          <dt className="text-[11px] uppercase tracking-wide text-white/38">时区</dt>
          <dd className="mt-0.5 font-mono text-[11px] text-white/55">{job.timezoneLabel}</dd>
        </div>
        {job.scheduleStartsAtIso && job.scheduleEndsAtIso ? (
          <div className="sm:col-span-2">
            <dt className="text-[11px] uppercase tracking-wide text-white/38">运行窗口</dt>
            <dd className="mt-0.5 tabular-nums text-white/78">
              {format(new Date(job.scheduleStartsAtIso), "yyyy年M月d日")} –{" "}
              {format(new Date(job.scheduleEndsAtIso), "yyyy年M月d日")}（不含结束日）
            </dd>
          </div>
        ) : null}
      </dl>

      {showStop || showDelete ? (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-white/[0.08] pt-3">
          {showStop ? (
            <button
              type="button"
              className={`${actionBtnBase} ${stopBtnClass} ${isStopPending ? "animate-pulse" : ""}`}
              onClick={() => onStopJob(job.id)}
              disabled={isStopPending || isDeletePending}
            >
              {isStopPending ? "正在停止…" : "停止定时任务"}
            </button>
          ) : null}
          {showDelete ? (
            <button
              type="button"
              className={`${actionBtnBase} ${deleteBtnClass} ${isDeletePending ? "animate-pulse" : ""}`}
              onClick={() => {
                if (window.confirm(DELETE_CONFIRM_TEXT)) {
                  onDeleteJob(job.id);
                }
              }}
              disabled={isDeletePending}
            >
              {isDeletePending ? "正在删除…" : "删除"}
            </button>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
