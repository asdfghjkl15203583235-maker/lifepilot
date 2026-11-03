"use client";

import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/authenticated-fetch";
import { clearSessionToken } from "@/lib/auth-session";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8081";

const LOGOUT_PATH = "/auth/logout";

interface AuthUser {
  id: string;
  email: string;
  name: string;
}

export function useAuth() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const checkAuth = useCallback(async () => {
    try {
      const response = await authenticatedFetch(`${API_BASE_URL}/auth/me`);
      if (!response.ok) {
        setUser(null);
        return;
      }
      const data = await response.json();
      setUser(data);
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void checkAuth();
  }, [checkAuth]);

  const logout = useCallback(async () => {
    try {
      await authenticatedFetch(`${API_BASE_URL}${LOGOUT_PATH}`, {
        method: "POST",
      });
    } catch {
      // Still clear local session so the UI can leave authenticated state
    } finally {
      clearSessionToken();
      setUser(null);
    }
  }, []);

  return { user, isAuthenticated: user !== null, isLoading, logout };
}
