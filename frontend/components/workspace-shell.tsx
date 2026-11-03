"use client";

import { useState, type ReactNode } from "react";

import { AgentTracesView } from "./agent-traces-view";
import { DashboardSidebar } from "./dashboard-sidebar";

export interface WorkspaceShellProps {
  topBar: ReactNode;
  children: ReactNode;
}

export function WorkspaceShell({ topBar, children }: WorkspaceShellProps) {
  const [isAgentTracesOpen, setIsAgentTracesOpen] = useState(false);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden lg:flex-row lg:items-stretch">
        <DashboardSidebar
          isAgentTracesOpen={isAgentTracesOpen}
          onAgentTracesToggle={() => setIsAgentTracesOpen((previous) => !previous)}
        />

        <div
          className={`flex min-h-0 min-w-0 bg-black/50 flex-1 flex-col overflow-hidden p-4 ${isAgentTracesOpen ? "bg-black/45" : ""}`}
        >
          {!isAgentTracesOpen ? (
            <div className="relative z-30 shrink-0 border-b border-white/10 pb-3">{topBar}</div>
          ) : null}

          <main className="relative z-0 flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
            {isAgentTracesOpen ? <AgentTracesView onExit={() => setIsAgentTracesOpen(false)} /> : children}
          </main>
        </div>
      </div>
    </div>
  );
}
