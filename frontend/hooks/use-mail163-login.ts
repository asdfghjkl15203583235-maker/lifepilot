"use client";

import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/authenticated-fetch";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

interface Mail163Status {
  isConnected: boolean;
  providerAccountId: string | null;
}

export function useMail163Login() {
  const [status, setStatus] = useState<Mail163Status>({ isConnected: false, providerAccountId: null });
  const [isLoading, setIsLoading] = useState(true);

  const loadStatus = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/mail163/status`);
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
    const timer = window.setTimeout(() => {
      void loadStatus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadStatus]);

  const connectMail163 = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/mail163/status`);
      if (response.status === 401) {
        // 登录前 status 也需要鉴权；直接走登录端点，由后端校验授权码。
        window.location.href = `${API_BASE_URL}/mail163/login`;
        return;
      }
      const data = response.ok ? await response.json() : null;
      if (!data?.is_connected) {
        window.alert("请在 backend/.env 配置 MAIL163_EMAIL 和 MAIL163_EMAIL_AUTH_CODE");
        return;
      }
      window.location.href = `${API_BASE_URL}/mail163/login`;
    } catch (error) {
      console.error("发起 163 邮箱登录失败：", error);
    }
  }, []);

  const disconnectMail163 = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/mail163/disconnect`, {
        method: "POST",
      });
      if (!response.ok) return;
      await loadStatus();
    } catch (error) {
      console.error("断开 163 邮箱失败：", error);
    }
  }, [loadStatus]);

  return { status, isLoading, connectMail163, disconnectMail163, refreshStatus: loadStatus };
}
