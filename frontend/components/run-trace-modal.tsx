"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { format } from "date-fns";
import { useEffect, type ReactNode } from "react";

import { useBodyScrollLock } from "@/hooks/use-body-scroll-lock";

import {
  RUN_OUTCOME_FAILED,
  RUN_OUTCOME_LABEL,
  RUN_OUTCOME_SKIPPED,
  RUN_OUTCOME_SUCCESS,
  TRACE_STEP_APPROVAL,
  TRACE_STEP_EMIT,
  TRACE_STEP_INTENT,
  TRACE_STEP_TOOL,
  type JobRun,
  type RunOutcome,
  type TraceStepKind,
} from "@/lib/mock-background-jobs";

const MODAL_SHELL_KEY = "run-trace-modal-shell";

const BACKDROP_CLASS =
  "absolute inset-0 bg-gradient-to-b from-zinc-950/80 via-zinc-950/70 to-black/75 backdrop-blur-md";

const PANEL_CLASS =
  // 多段阴影（含 inset）走任意 CSS 属性：用 shadow 工具类会让 Tailwind
  // 生成空的 `--tw-shadow` 段，Turbopack 解析 CSS 会直接失败。
  "relative z-10 flex max-h-[min(92vh,780px)] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-white/[0.09] bg-gradient-to-b from-zinc-800/[0.55] via-zinc-900/95 to-zinc-950 [box-shadow:0_28px_90px_rgba(0,0,0,0.45),inset_0_1px_0_rgba(255,255,255,0.07),inset_0_-1px_0_rgba(0,0,0,0.35)] ring-1 ring-white/[0.05]";

const STEP_KIND_LABEL: Record<TraceStepKind, string> = {
  [TRACE_STEP_INTENT]: "意图",
  [TRACE_STEP_TOOL]: "工具",
  [TRACE_STEP_APPROVAL]: "审批",
  [TRACE_STEP_EMIT]: "输出",
};

const STEP_KIND_BADGE_CLASS: Record<TraceStepKind, string> = {
  [TRACE_STEP_INTENT]: "border-violet-300/25 bg-violet-500/18 text-violet-50",
  [TRACE_STEP_TOOL]: "border-sky-300/25 bg-sky-500/18 text-sky-50",
  [TRACE_STEP_APPROVAL]: "border-amber-300/25 bg-amber-500/18 text-amber-50",
  [TRACE_STEP_EMIT]: "border-emerald-300/25 bg-emerald-500/18 text-emerald-50",
};

const STEP_CARD_ACCENT: Record<TraceStepKind, string> = {
  [TRACE_STEP_INTENT]: "border-l-emerald-400/45",
  [TRACE_STEP_TOOL]: "border-l-sky-400/45",
  [TRACE_STEP_APPROVAL]: "border-l-amber-400/45",
  [TRACE_STEP_EMIT]: "border-l-violet-400/45",
};

const OUTCOME_SUMMARY_SURFACE: Record<RunOutcome, string> = {
  [RUN_OUTCOME_SUCCESS]:
    "border-emerald-400/20 bg-gradient-to-br from-emerald-500/[0.12] to-emerald-950/20 ring-1 ring-emerald-400/15",
  [RUN_OUTCOME_FAILED]:
    "border-rose-400/20 bg-gradient-to-br from-rose-500/[0.14] to-rose-950/25 ring-1 ring-rose-400/15",
  [RUN_OUTCOME_SKIPPED]:
    "border-amber-400/18 bg-gradient-to-br from-amber-500/[0.10] to-zinc-900/50 ring-1 ring-amber-400/12",
};

function IconCpu({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z"
      />
    </svg>
  );
}

function IconChartBadge({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
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

function IconClipboard({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
    </svg>
  );
}

function IconRoute({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
    </svg>
  );
}

function IconSparkles({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"
      />
    </svg>
  );
}

function IconWrench({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
      />
      <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}

function IconShield({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  );
}

function IconPaperAirplane({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
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

function traceStepIcon(kind: TraceStepKind): ReactNode {
  const iconClass = "h-4 w-4";
  switch (kind) {
    case TRACE_STEP_INTENT:
      return <IconSparkles className={iconClass} aria-hidden />;
    case TRACE_STEP_TOOL:
      return <IconWrench className={iconClass} aria-hidden />;
    case TRACE_STEP_APPROVAL:
      return <IconShield className={iconClass} aria-hidden />;
    case TRACE_STEP_EMIT:
      return <IconPaperAirplane className={iconClass} aria-hidden />;
    default:
      return null;
  }
}

export interface RunTraceModalProps {
  readonly run: JobRun | null;
  readonly onClose: () => void;
}

export function RunTraceModal({ run, onClose }: RunTraceModalProps) {
  const isOpen = run !== null;
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

  return (
    <AnimatePresence>
      {run ? (
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
            aria-labelledby="run-trace-modal-title"
            className={PANEL_CLASS}
            initial={{ opacity: 0, y: 18, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 14, scale: 0.985 }}
            transition={panelTransition}
          >
            <div className="flex shrink-0 items-start justify-between gap-3 border-b border-white/[0.07] bg-gradient-to-r from-violet-500/12 via-transparent to-sky-500/10 px-5 py-4">
              <div className="flex min-w-0 gap-3">
                <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-violet-400/30 to-sky-500/25 text-white shadow-inner ring-1 ring-white/10">
                  <IconCpu className="h-6 w-6" aria-hidden />
                </span>
                <div className="min-w-0">
                  <h2 id="run-trace-modal-title" className="truncate text-lg font-semibold tracking-tight text-white">
                    代理追踪
                  </h2>
                  <p className="mt-1 truncate text-[13px] text-white/75">{run.jobTitle}</p>
                  <p className="mt-0.5 font-mono text-[11px] text-white/45">
                    {run.id} · {run.jobId}
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

            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-6">
              <dl
                className={`grid gap-4 rounded-2xl border border-white/[0.08] p-4 text-[13px] sm:grid-cols-2 ${OUTCOME_SUMMARY_SURFACE[run.outcome]}`}
              >
                <div className="flex gap-3 sm:col-span-1">
                  <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/10 text-white ring-1 ring-white/15">
                    <IconChartBadge className="h-4 w-4" aria-hidden />
                  </span>
                  <div>
                    <dt className="text-[11px] font-medium uppercase tracking-wide text-white/50">结果</dt>
                    <dd className="mt-1 font-semibold text-white">{RUN_OUTCOME_LABEL[run.outcome]}</dd>
                  </div>
                </div>
                <div className="flex gap-3 sm:col-span-1">
                  <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/10 text-white ring-1 ring-white/15">
                    <IconClock className="h-4 w-4" aria-hidden />
                  </span>
                  <div>
                    <dt className="text-[11px] font-medium uppercase tracking-wide text-white/50">时间窗</dt>
                    <dd className="mt-1 tabular-nums text-white/82">
                      {format(new Date(run.startedAtIso), "MMM d · h:mm:ss a")}
                      {" → "}
                      {format(new Date(run.finishedAtIso), "h:mm:ss a")}
                    </dd>
                  </div>
                </div>
                <div className="flex gap-3 sm:col-span-2">
                  <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white/10 text-white ring-1 ring-white/15">
                    <IconClipboard className="h-4 w-4" aria-hidden />
                  </span>
                  <div className="min-w-0">
                    <dt className="text-[11px] font-medium uppercase tracking-wide text-white/50">摘要</dt>
                    <dd
                      className="mt-1 line-clamp-2 leading-relaxed text-white/78"
                      title={run.detail}
                    >
                      {run.detail}
                    </dd>
                  </div>
                </div>
              </dl>

              <div className="mt-8 flex flex-wrap items-start gap-3">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-fuchsia-500/25 to-violet-600/25 text-fuchsia-100 ring-1 ring-fuchsia-400/25">
                  <IconRoute className="h-5 w-5" aria-hidden />
                </span>
                <div>
                  <h3 className="text-sm font-semibold text-white/92">执行追踪</h3>
                  <p className="mt-0.5 max-w-prose text-[12px] leading-relaxed text-white/52">
                    按顺序展示意图、工具、审批和输出步骤，并标明负责每个步骤的代理。
                  </p>
                </div>
              </div>

              <ol className="mt-5 space-y-3">
                {run.traces.map((step, index) => (
                  <li key={step.id}>
                    <div
                      className={`rounded-xl border border-white/[0.07] bg-zinc-900/45 p-4 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] ring-1 ring-black/20 ${STEP_CARD_ACCENT[step.stepKind]} border-l-[3px]`}
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[11px] tabular-nums text-white/40">
                          {(index + 1).toString().padStart(2, "0")}
                        </span>
                        <span className="flex h-7 w-7 items-center justify-center rounded-md bg-white/[0.06] text-white/85 ring-1 ring-white/[0.08]">
                          {traceStepIcon(step.stepKind)}
                        </span>
                        <span
                          className={`rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${STEP_KIND_BADGE_CLASS[step.stepKind]}`}
                        >
                          {STEP_KIND_LABEL[step.stepKind]}
                        </span>
                        <span className="text-[13px] font-semibold text-white/92">{step.agentLabel}</span>
                      </div>
                      <p className="mt-2 text-[13px] font-medium text-white/88">{step.title}</p>
                      <p
                        className="mt-1 line-clamp-2 text-[12px] leading-relaxed text-white/58"
                        title={step.detail}
                      >
                        {step.detail}
                      </p>
                      <p className="mt-2 flex items-center gap-1.5 font-mono text-[11px] tabular-nums text-white/38">
                        <IconClock className="h-3.5 w-3.5 shrink-0 opacity-70" aria-hidden />
                        {format(new Date(step.atIso), "MMM d · HH:mm:ss.SSS")}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
