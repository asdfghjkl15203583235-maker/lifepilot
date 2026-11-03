"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { NotificationsDashboard } from "./notifications-dashboard";
import { WorkspaceShell } from "./workspace-shell";
import { useNotificationsInbox } from "@/hooks/use-notifications-inbox";

export function NotificationsWorkspace() {
  const router = useRouter();
  const { items, isLoading, loadError, markAsRead, markAllAsRead, convertToTask } =
    useNotificationsInbox();

  const handleCreateTask = async (notificationId: string) => {
    try {
      const created = await convertToTask(notificationId);
      router.push(`/tasks/${created.task_id}`);
    } catch (error) {
      console.error("把通知转成任务失败", error);
    }
  };

  return (
    <WorkspaceShell
      topBar={
        <div className="flex min-h-10 flex-wrap items-center gap-2 rounded-xl border border-white/25 bg-white/12 px-3 py-2 shadow-[inset_0_1px_0_rgba(255,255,255,0.08)] backdrop-blur-md sm:gap-3">
          <Link
            href="/"
            className="text-[13px] font-medium text-black underline-offset-4 transition hover:text-white hover:underline"
          >
            ← 工作区
          </Link>
          <span className="text-white/30" aria-hidden>
            |
          </span>
          <span className="text-[13px] font-semibold tracking-tight text-white/90">通知</span>
        </div>
      }
    >
      <NotificationsDashboard
        items={items}
        isLoading={isLoading}
        loadError={loadError}
        onMarkItemRead={markAsRead}
        onMarkAllRead={markAllAsRead}
        onCreateTask={(notificationId) => void handleCreateTask(notificationId)}
        onOpenTask={(taskId) => router.push(`/tasks/${taskId}`)}
      />
    </WorkspaceShell>
  );
}
