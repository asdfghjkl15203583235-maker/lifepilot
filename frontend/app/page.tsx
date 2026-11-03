"use client";

import Image from "next/image";

import { GlassWorkspace } from "../components/glass-workspace";
import { useAuth } from "@/hooks/use-auth";
import { useMail163Login } from "@/hooks/use-mail163-login";

function LoginScreen({ onConnectMail163 }: { onConnectMail163: () => void }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-10 px-4">
      <Image
        src="/assets/orb.png"
        alt=""
        width={152}
        height={152}
        priority
        aria-hidden
        className="h-40 w-40 object-contain drop-shadow-[0_0_28px_rgba(168,85,247,0.55)]"
      />

      <div className="flex flex-col items-center gap-3 text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-3xl">LifePilot</h1>
        <p className="max-w-sm text-sm text-white/60">
          使用 163 邮箱登录即可开始。你的 AI 规划助手会协助管理收件箱、日历、会议、记忆和生活任务。
        </p>
      </div>

      <button
        type="button"
        onClick={onConnectMail163}
        className="inline-flex items-center gap-3 rounded-full border border-white/20 bg-white/10 px-6 py-3 text-sm font-medium text-white shadow-[0_8px_32px_-8px_rgba(124,58,237,0.5)] backdrop-blur-md transition hover:border-white/30 hover:bg-white/15"
      >
        <Image
          src="/icons/integrations/mail163.svg"
          alt=""
          width={20}
          height={20}
          className="h-5 w-5 object-contain"
          unoptimized
        />
        163 邮箱登录
      </button>
    </div>
  );
}

export default function Home() {
  const { isAuthenticated, isLoading } = useAuth();
  const { connectMail163 } = useMail163Login();

  return (
    <div className="wrap box-border flex h-dvh max-h-dvh min-h-0 w-full flex-col overflow-hidden bg-cover bg-center bg-no-repeat bg-ai-hero">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden border border-white/25 bg-white/10 shadow-[0_8px_32px_rgba(0,0,0,0.12)] backdrop-blur-xl backdrop-saturate-150">
        {isLoading ? null : isAuthenticated ? <GlassWorkspace /> : <LoginScreen onConnectMail163={connectMail163} />}
      </div>
    </div>
  );
}
