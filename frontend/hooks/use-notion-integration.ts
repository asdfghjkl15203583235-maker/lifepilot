"use client";

import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

interface NotionStatus {
  isConnected: boolean;
  providerAccountId: string | null;
  workspaceName: string | null;
}

export function useNotionIntegration() {
  const [status, setStatus] = useState<NotionStatus>({
    isConnected: false,
    providerAccountId: null,
    workspaceName: null,
  });
  const [isLoading, setIsLoading] = useState(true);

  const loadStatus = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/notion/status`);
      if (!response.ok) {
        setIsLoading(false);
        return;
      }
      const data = await response.json();
      setStatus({
        isConnected: data.is_connected,
        providerAccountId: data.provider_account_id,
        workspaceName: data.workspace_name,
      });
    } catch {
      // Not authenticated or network error — stay disconnected
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const connectNotion = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/notion/auth-url`);
      if (!response.ok) return;
      const { auth_url } = await response.json();
      window.location.href = auth_url;
    } catch (error) {
      console.error("发起 Notion 连接失败：", error);
    }
  }, []);

  const disconnectNotion = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/notion/disconnect`, {
        method: "POST",
      });
      if (!response.ok) return;
      await loadStatus();
    } catch (error) {
      console.error("断开 Notion 失败：", error);
    }
  }, [loadStatus]);

  return { status, isLoading, connectNotion, disconnectNotion, refreshStatus: loadStatus };
}
