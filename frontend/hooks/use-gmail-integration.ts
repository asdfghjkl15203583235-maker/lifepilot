"use client";

import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

interface GmailStatus {
  isConnected: boolean;
  providerAccountId: string | null;
}

export function useGmailIntegration() {
  const [status, setStatus] = useState<GmailStatus>({ isConnected: false, providerAccountId: null });
  const [isLoading, setIsLoading] = useState(true);

  const loadStatus = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/gmail/status`);
      if (!response.ok) {
        setIsLoading(false);
        return;
      }
      const data = await response.json();
      setStatus({
        isConnected: data.is_connected,
        providerAccountId: data.provider_account_id,
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

  const connectGmail = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/gmail/auth-url`);
      if (!response.ok) return;
      const { auth_url } = await response.json();
      window.location.href = auth_url;
    } catch (error) {
      console.error("发起 Gmail 连接失败：", error);
    }
  }, []);

  const disconnectGmail = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/gmail/disconnect`, {
        method: "POST",
      });
      if (!response.ok) return;
      await loadStatus();
    } catch (error) {
      console.error("断开 Gmail 失败：", error);
    }
  }, [loadStatus]);

  return { status, isLoading, connectGmail, disconnectGmail, refreshStatus: loadStatus };
}
