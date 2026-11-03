"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useEffect } from "react";

import { ScheduleJobCard } from "./schedule-job-card";
import { useBodyScrollLock } from "@/hooks/use-body-scroll-lock";
import {
  partitionSchedulesByLifecycle,
  type BackgroundJob,
} from "@/lib/mock-background-jobs";

const MODAL_SHELL_KEY = "schedule-jobs-all-modal-shell";

const BACKDROP_CLASS =
  "absolute inset-0 bg-gradient-to-b from-zinc-950/80 via-zinc-950/70 to-black/75 backdrop-blur-md";

const PANEL_CLASS =
  // 多段阴影（含 inset）必须走任意 CSS 属性：用 shadow 工具类会让 Tailwind
  // 生成的 `--tw-shadow` 出现空段，Turbopack 解析 CSS 直接失败。
  "relative z-10 flex max-h-[min(92vh,840px)] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-white/[0.09] bg-gradient-to-b from-zinc-800/[0.55] via-zinc-900/95 to-zinc-950 [box-shadow:0_28px_90px_rgba(0,0,0,0.45),inset_0_1px_0_rgba(255,255,255,0.07),inset_0_-1px_0_rgba(0,0,0,0.35)] ring-1 ring-white/[0.05]";

const HEADER_BAR_CLASS =
  "border-b border-white/[0.07] bg-gradient-to-r from-emerald-500/12 via-transparent to-violet-500/10 px-5 py-4";

const SECTION_ACTIVE_WRAP =
  "rounded-2xl border border-emerald-400/15 bg-emerald-500/[0.06] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]";

const SECTION_ENDED_WRAP =
  "rounded-2xl border border-violet-400/12 bg-violet-500/[0.05] p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]";

const EMPTY_STATE_CLASS =
  "rounded-xl border border-dashed border-white/[0.12] bg-white/[0.03] px-4 py-8 text-center text-[13px] text-white/55";

function IconLayers({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 17l9 5 9-5"
      />
    </svg>
  );
}

function IconPlayCircle({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-5.197-3.498A1 1 0 008 8.503v7.054a1 1 0 001.555.832l5.197-3.498a1 1 0 000-1.664z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  );
}

function IconArchive({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4" />
    </svg>
  );
}

function IconX({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
    </svg>
  );
}

export interface ScheduleJobsAllModalProps {
  readonly isOpen: boolean;
  readonly jobs: readonly BackgroundJob[];
  readonly onClose: () => void;
  readonly canManage?: boolean;
  readonly onStopJob?: (jobId: string) => void;
  readonly onDeleteJob?: (jobId: string) => void;
  readonly pendingStopJobId?: string | null;
  readonly pendingDeleteJobId?: string | null;
}

export function ScheduleJobsAllModal({
  isOpen,
  jobs,
  onClose,
  canManage = false,
  onStopJob,
  onDeleteJob,
  pendingStopJobId = null,
  pendingDeleteJobId = null,
}: ScheduleJobsAllModalProps) {
  const prefersReducedMotion = useReducedMotion();
  const shellTransition = prefersReducedMotion ? { duration: 0.01 } : { duration: 0.22 };
  const panelTransition = prefersReducedMotion ? { duration: 0.01 } : { type: "spring" as const, damping: 28, stiffness: 320 };

  useBodyScrollLock(isOpen);

  useEffect(() => {
    if (!isOpen) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isOpen, onClose]);

  const { ongoing, ended } = partitionSchedulesByLifecycle(jobs);

  return (
    <AnimatePresence>
      {isOpen ? (
        <motion.div
          key={MODAL_SHELL_KEY}
          className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center"
          role="presentation"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={shellTransition}
        >
          <button type="button" aria-label="关闭对话框" className={BACKDROP_CLASS} onClick={onClose} />

          <motion.div
            role="dialog"
            aria-modal="true"
            aria-labelledby="schedule-all-modal-title"
            className={PANEL_CLASS}
            initial={{ opacity: 0, y: 20, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.985 }}
            transition={panelTransition}
          >
            <div className={`flex shrink-0 items-start justify-between gap-3 ${HEADER_BAR_CLASS}`}>
              <div className="flex min-w-0 gap-3">
                <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-emerald-400/25 to-violet-500/25 text-emerald-100 shadow-inner ring-1 ring-white/10">
                  <IconLayers className="h-6 w-6" aria-hidden />
                </span>
                <div className="min-w-0">
                  <h2 id="schedule-all-modal-title" className="text-lg font-semibold tracking-tight text-white">
                    全部定时任务
                  </h2>
                  <p className="mt-1 text-[13px] leading-snug text-white/65">
                    运行中和已暂停的任务在一起显示。已结束任务保留在下方供审计。
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                className="flex shrink-0 items-center gap-2 rounded-xl border border-white/[0.10] bg-white/[0.06] px-3 py-2 text-[13px] font-medium text-white/90 shadow-sm transition hover:border-white/[0.14] hover:bg-white/[0.10]"
              >
                <IconX className="h-4 w-4 text-white/70" aria-hidden />
                关闭
              </button>
            </div>

            <div className="min-h-0 flex-1 space-y-8 overflow-y-auto overscroll-contain px-5 py-6">
              <section aria-labelledby="schedules-active-heading" className={SECTION_ACTIVE_WRAP}>
                <div className="mb-4 flex flex-wrap items-start gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-500/20 text-emerald-200 ring-1 ring-emerald-400/25">
                    <IconPlayCircle className="h-5 w-5" aria-hidden />
                  </span>
                  <div>
                    <h3 id="schedules-active-heading" className="text-sm font-semibold text-emerald-100/95">
                      运行中的定时任务
                    </h3>
                    <p className="mt-0.5 max-w-prose text-[12px] leading-relaxed text-emerald-100/55">
                      包含已暂停的任务——它们会一直保留，直到你结束或删除。
                    </p>
                  </div>
                </div>
                {ongoing.length === 0 ? (
                  <p className={EMPTY_STATE_CLASS}>暂无运行中的定时任务。</p>
                ) : (
                  <ul className="grid gap-3 sm:grid-cols-2">
                    {ongoing.map((job) => (
                      <li key={job.id}>
                        <ScheduleJobCard
                          job={job}
                          density="compact"
                          surface="modal"
                          canManage={canManage}
                          onStopJob={onStopJob}
                          onDeleteJob={onDeleteJob}
                          isStopPending={pendingStopJobId === job.id}
                          isDeletePending={pendingDeleteJobId === job.id}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>

              <section aria-labelledby="schedules-ended-heading" className={SECTION_ENDED_WRAP}>
                <div className="mb-4 flex flex-wrap items-start gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-violet-500/20 text-violet-200 ring-1 ring-violet-400/25">
                    <IconArchive className="h-5 w-5" aria-hidden />
                  </span>
                  <div>
                    <h3 id="schedules-ended-heading" className="text-sm font-semibold text-violet-100/95">
                      已结束的定时任务
                    </h3>
                    <p className="mt-0.5 max-w-prose text-[12px] leading-relaxed text-violet-100/55">
                      已完成、已取消或已停用的自动化——保留用于历史查看。
                    </p>
                  </div>
                </div>
                {ended.length === 0 ? (
                  <p className={EMPTY_STATE_CLASS}>暂无已结束的定时任务。</p>
                ) : (
                  <ul className="grid gap-3 sm:grid-cols-2">
                    {ended.map((job) => (
                      <li key={job.id}>
                        <ScheduleJobCard
                          job={job}
                          density="compact"
                          surface="modal"
                          canManage={canManage}
                          onStopJob={onStopJob}
                          onDeleteJob={onDeleteJob}
                          isStopPending={pendingStopJobId === job.id}
                          isDeletePending={pendingDeleteJobId === job.id}
                        />
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
