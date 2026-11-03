"use client";

import Link from "next/link";

import { TasksDashboard } from "./tasks-dashboard";
import { WorkspaceShell } from "./workspace-shell";

export function TasksWorkspace() {
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
          <span className="text-[13px] font-semibold tracking-tight text-white/90">任务</span>
        </div>
      }
    >
      <TasksDashboard />
    </WorkspaceShell>
  );
}
