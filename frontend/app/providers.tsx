"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

import { ReminderToastHost } from "@/components/reminder-toast-host";
import { consumeOAuthFragment } from "@/lib/auth-session";

const STALE_TIME_MS = 30_000;

export function AppProviders({ children }: Readonly<{ children: ReactNode }>) {
  if (typeof window !== "undefined") {
    consumeOAuthFragment();
  }

  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: STALE_TIME_MS,
            retry: 1,
          },
        },
      }),
  );

  return (
    <QueryClientProvider client={client}>
      {children}
      <ReminderToastHost />
    </QueryClientProvider>
  );
}
