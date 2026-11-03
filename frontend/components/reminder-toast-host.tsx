"use client";

import { formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import { AnimatePresence, motion } from "framer-motion";

import { useReminderToasts, type ReminderToastItem } from "@/hooks/use-reminder-toasts";
import {
  NOTIFICATION_CATEGORY_EMAIL,
  NOTIFICATION_CATEGORY_LABEL,
} from "@/lib/mock-notifications";

const SNOOZE_MINUTES = 15;

function IconTerminal({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M8 9l3 3-3 3m5 0h3M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
      />
    </svg>
  );
}

function IconEnvelope({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 7l9 6 9-6M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
      />
    </svg>
  );
}

function isEmailToast(toast: ReminderToastItem): boolean {
  return toast.category === NOTIFICATION_CATEGORY_EMAIL;
}

function ToastActions({
  toast,
  onOpenTask,
  onConvertToTask,
  onAcknowledge,
  onSnooze,
  onCompleteTask,
  onDismiss,
}: {
  toast: ReminderToastItem;
  onOpenTask: (toast: ReminderToastItem) => void;
  onConvertToTask: (toast: ReminderToastItem) => void;
  onAcknowledge: (toast: ReminderToastItem) => void;
  onSnooze: (toast: ReminderToastItem) => void;
  onCompleteTask: (toast: ReminderToastItem) => void;
  onDismiss: (toast: ReminderToastItem) => void;
}) {
  if (isEmailToast(toast)) {
    return (
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {toast.taskId ? (
          <button
            type="button"
            onClick={() => onOpenTask(toast)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/30 bg-sky-500/15 px-3 py-1.5 text-[12px] font-medium text-sky-100 transition hover:bg-sky-500/25"
          >
            查看任务
          </button>
        ) : (
          <button
            type="button"
            onClick={() => onConvertToTask(toast)}
            className="inline-flex items-center gap-1.5 rounded-lg border border-sky-400/30 bg-sky-500/15 px-3 py-1.5 text-[12px] font-medium text-sky-100 transition hover:bg-sky-500/25"
          >
            转成任务
          </button>
        )}
        <button
          type="button"
          onClick={() => onAcknowledge(toast)}
          className="ml-auto rounded-lg px-2 py-1.5 text-[12px] font-medium text-white/50 transition hover:bg-white/10 hover:text-white/80"
        >
          关闭
        </button>
      </div>
    );
  }

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => onCompleteTask(toast)}
        disabled={!toast.taskId}
        className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-400/30 bg-emerald-500/15 px-3 py-1.5 text-[12px] font-medium text-emerald-100 transition hover:bg-emerald-500/25 disabled:cursor-not-allowed disabled:opacity-40"
      >
        完成任务
      </button>
      <button
        type="button"
        onClick={() => onSnooze(toast)}
        disabled={!toast.reminderId}
        className="rounded-lg border border-white/15 bg-white/5 px-3 py-1.5 text-[12px] font-medium text-white/85 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
      >
        稍后提醒
      </button>
      <button
        type="button"
        onClick={() => onDismiss(toast)}
        className="ml-auto rounded-lg px-2 py-1.5 text-[12px] font-medium text-white/50 transition hover:bg-white/10 hover:text-white/80"
      >
        关闭
      </button>
    </div>
  );
}

export function ReminderToastHost({ pollIntervalMs = 20_000 }: { pollIntervalMs?: number }) {
  const {
    toasts,
    queuedCount,
    dismiss,
    acknowledge,
    openTask,
    convertToTask,
    snooze,
    completeTask,
    dismissAll,
  } = useReminderToasts(pollIntervalMs);

  if (toasts.length === 0) {
    return null;
  }

  return (
    <div
      className="pointer-events-none fixed bottom-5 right-5 z-[100] flex w-[min(24rem,calc(100vw-2.5rem))] flex-col gap-3"
      role="region"
      aria-label="提醒与通知"
    >
      <AnimatePresence initial={false}>
        {toasts.map((toast) => {
          const created = new Date(toast.createdAtIso);
          const relative = formatDistanceToNow(created, { addSuffix: true, locale: zhCN });
          const emailToast = isEmailToast(toast);
          return (
            <motion.div
              key={toast.notificationId}
              layout
              initial={{ opacity: 0, y: 24, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 12, scale: 0.98 }}
              transition={{ type: "spring", stiffness: 380, damping: 30 }}
              className={
                emailToast
                  ? "pointer-events-auto flex gap-3 rounded-2xl border border-sky-400/30 bg-zinc-950/95 p-4 shadow-[0_12px_40px_-8px_rgba(56,189,248,0.5)] backdrop-blur-xl"
                  : "pointer-events-auto flex gap-3 rounded-2xl border border-violet-400/30 bg-zinc-950/95 p-4 shadow-[0_12px_40px_-8px_rgba(124,58,237,0.5)] backdrop-blur-xl"
              }
            >
              <span
                className={
                  emailToast
                    ? "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-sky-500/25 text-sky-100 ring-1 ring-sky-400/30"
                    : "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-violet-500/25 text-violet-100 ring-1 ring-violet-400/30"
                }
                aria-hidden
              >
                {emailToast ? <IconEnvelope className="h-4.5 w-4.5" /> : <IconTerminal className="h-4.5 w-4.5" />}
              </span>
              <div className="min-w-0 flex-1">
                <p
                  className={
                    emailToast
                      ? "text-[10px] uppercase tracking-wide text-sky-300/70"
                      : "text-[10px] uppercase tracking-wide text-violet-300/70"
                  }
                >
                  {NOTIFICATION_CATEGORY_LABEL[toast.category]}
                </p>
                <p className="mt-0.5 text-[13px] font-semibold leading-snug text-white">{toast.title}</p>
                <p className="mt-1 line-clamp-3 whitespace-pre-line text-[12px] leading-relaxed text-white/65">
                  {toast.body}
                </p>
                <p className="mt-1.5 text-[10px] tracking-wide text-white/40">{relative}</p>
                <ToastActions
                  toast={toast}
                  onOpenTask={openTask}
                  onConvertToTask={(item) => void convertToTask(item)}
                  onAcknowledge={acknowledge}
                  onSnooze={(item) => void snooze(item, SNOOZE_MINUTES)}
                  onCompleteTask={(item) => void completeTask(item)}
                  onDismiss={(item) => dismiss(item.notificationId)}
                />
              </div>
              <span
                className={
                  emailToast
                    ? "reminder-breathing-dot mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full bg-sky-400"
                    : "reminder-breathing-dot mt-0.5 h-2.5 w-2.5 shrink-0 rounded-full bg-violet-400"
                }
                aria-hidden
              />
            </motion.div>
          );
        })}
      </AnimatePresence>
      <div className="pointer-events-auto flex items-center gap-2 self-end">
        {queuedCount > 0 ? (
          <span className="rounded-lg border border-white/10 bg-black/50 px-2.5 py-1 text-[11px] text-white/45 backdrop-blur-md">
            还有 {queuedCount} 条
          </span>
        ) : null}
        <button
          type="button"
          onClick={dismissAll}
          className="rounded-lg border border-white/10 bg-black/50 px-2.5 py-1 text-[11px] text-white/50 backdrop-blur-md transition hover:bg-black/70 hover:text-white/70"
        >
          全部关闭
        </button>
      </div>
    </div>
  );
}
