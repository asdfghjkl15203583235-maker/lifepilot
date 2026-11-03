"use client";

import { useRef, useState } from "react";

import { approvePendingAction, rejectPendingAction, type PendingActionRecord } from "@/lib/pending-actions";

interface PendingActionCardProps {
  readonly action: PendingActionRecord;
  /** Invoked after a successful approve/reject so the parent can refresh data
   *  and mark the stored action as settled (otherwise switching tabs re-renders
   *  it as "等待确认" and the user confirms the same draft twice). */
  readonly onResult?: (status: "approved" | "rejected") => void;
}

function hashCode(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) {
    hash = (hash << 5) - hash + value.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

const ACCENT_CLASSES = [
  "border-sky-400/25",
  "border-emerald-400/25",
  "border-violet-400/25",
  "border-amber-400/25",
] as const;

/** Render a single high-risk write draft awaiting user confirmation. */
export function PendingActionCard({ action, onResult }: PendingActionCardProps) {
  const accent = ACCENT_CLASSES[hashCode(action.id) % ACCENT_CLASSES.length];
  const [busy, setBusy] = useState<"approving" | "rejecting" | null>(null);
  const [done, setDone] = useState<null | "approved" | "rejected" | "failed">(null);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);
  // state 更新是异步的，双击会在两次渲染之间穿过 `busy !== null` 判断，
  // 于是同一个草案被提交两次。用同步 ref 做真正的互斥锁。
  const inFlightRef = useRef(false);

  const isEmail = action.actionType === "email_send" || action.actionType === "email_reply";
  const isDelete = action.actionType === "task_delete";
  const payload = action.payload ?? {};

  // 卡片本身没有卸载时（父组件不再按 status 过滤），终态一律在这里渲染：
  // 已确认的草案必须留在原位显示"已发送"，而不是从界面上凭空消失。
  const settled = done ?? (action.status === "pending" ? null : action.status);

  if (settled === "approved") {
    const sent = result ?? {};
    const to = String(sent.to ?? payload.to ?? "");
    const subject = String(sent.subject ?? payload.subject ?? "");
    const taskTitle = String(sent.title ?? payload.task_title ?? payload.task_id ?? "");
    const summaryText = isEmail
      ? `邮件已发送${to ? `至 ${to}` : ""}${subject ? `（主题：${subject}）` : ""}。`
      : isDelete
        ? `已删除任务「${taskTitle}」。`
        : `已更新任务「${taskTitle}」。`;
    return (
      <div className={`rounded-xl border bg-zinc-900/45 p-3 text-[13px] text-emerald-200/90 ${accent}`}>
        {isEmail ? "已发送。" : "已确认并执行成功。"}
        <span className="mt-1 block text-[12px] text-emerald-100/60">{summaryText}</span>
      </div>
    );
  }
  if (settled === "rejected") {
    return (
      <div className={`rounded-xl border bg-zinc-900/45 p-3 text-[13px] text-white/55 ${accent}`}>
        已拒绝，未执行该操作。
      </div>
    );
  }
  if (settled === "expired") {
    return (
      <div className={`rounded-xl border bg-zinc-900/45 p-3 text-[13px] text-white/45 ${accent}`}>
        该操作已过期，未执行。
      </div>
    );
  }
  // 历史里读到 failed（不是本次点击产生的）说明后端已判失败，重试只会拿到 410。
  if (settled === "failed" && done !== "failed") {
    return (
      <div className={`rounded-xl border bg-zinc-900/45 p-3 text-[13px] text-rose-200/80 ${accent}`}>
        该操作执行失败，未生效。
      </div>
    );
  }

  const handleApprove = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy("approving");
    setError(null);
    try {
      const response = await approvePendingAction(action.id);
      setResult(response.result);
      setDone("approved");
      onResult?.("approved");
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认执行失败。");
      setDone("failed");
    } finally {
      inFlightRef.current = false;
      setBusy(null);
    }
  };

  const handleReject = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy("rejecting");
    setError(null);
    try {
      await rejectPendingAction(action.id);
      setDone("rejected");
      onResult?.("rejected");
    } catch (err) {
      setError(err instanceof Error ? err.message : "拒绝失败。");
    } finally {
      inFlightRef.current = false;
      setBusy(null);
    }
  };

  const changeList = Object.entries(payload?.changes ?? {}) as [string, unknown][];

  return (
    <div className={`mt-2 rounded-xl border bg-zinc-900/45 p-3.5 backdrop-blur-md ${accent}`}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-[14px] font-semibold text-white">{action.title}</p>
        <span className="shrink-0 rounded-full border border-amber-400/30 bg-amber-500/15 px-2 py-0.5 text-[10px] font-medium text-amber-100">
          等待确认
        </span>
      </div>

      {isEmail ? (
        <dl className="mt-2.5 space-y-1.5 text-[12px] leading-snug">
          <div className="flex gap-2">
            <dt className="shrink-0 text-white/40">收件人</dt>
            <dd className="text-white/85">{String(payload.to ?? "")}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="shrink-0 text-white/40">主题</dt>
            <dd className="text-white/85">{String(payload.subject ?? "")}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="shrink-0 text-white/40">内容</dt>
            <dd className="whitespace-pre-wrap text-white/75">{String(payload.body ?? "")}</dd>
          </div>
        </dl>
      ) : null}

      {action.actionType === "task_update" ? (
        <dl className="mt-2.5 space-y-1.5 text-[12px] leading-snug">
          <div className="flex gap-2">
            <dt className="shrink-0 text-white/40">目标任务</dt>
            <dd className="text-white/85">{String(payload.task_title ?? payload.task_id ?? "")}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="shrink-0 text-white/40">计划变更</dt>
            <dd className="text-white/75">
              {changeList.length > 0 ? (
                <span className="whitespace-pre-wrap">
                  {changeList.map(([k, v]) => `${k}: ${String(v)}`).join("\n")}
                </span>
              ) : (
                "—"
              )}
            </dd>
          </div>
        </dl>
      ) : null}

      {isDelete ? (
        <div className="mt-2.5 text-[12px] leading-snug">
          <span className="text-white/40">将永久删除任务：</span>
          <span className="text-rose-200/95">{String(payload.task_title ?? payload.task_id ?? "")}</span>
        </div>
      ) : null}

      {error ? <p className="mt-2 text-[12px] text-rose-300">{error}</p> : null}

      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/[0.08] pt-3">
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void handleApprove()}
          className={`rounded-lg border border-emerald-400/25 px-3 py-1.5 text-[12px] font-medium text-emerald-100 transition enabled:hover:border-emerald-400/50 enabled:hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:opacity-50 ${
            busy === "approving" ? "animate-pulse" : ""
          }`}
        >
          {busy === "approving" ? "正在执行…" : "确认执行"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => void handleReject()}
          className="rounded-lg border border-white/15 px-3 py-1.5 text-[12px] font-medium text-white/75 transition enabled:hover:border-white/30 enabled:hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy === "rejecting" ? "正在处理…" : "拒绝"}
        </button>
      </div>
      <p className="mt-2 text-[10px] text-white/35">此操作经你确认后才会真正执行。</p>
    </div>
  );
}