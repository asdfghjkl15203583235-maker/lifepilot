import { NotificationsWorkspace } from "@/components/notifications-workspace";

export default function NotificationsPage() {
  return (
    <div className="wrap box-border flex h-dvh max-h-dvh min-h-0 w-full flex-col overflow-hidden bg-cover bg-center bg-no-repeat bg-[url('/assets/ai-bg.jpg')]">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden border border-white/25 bg-white/10 shadow-[0_8px_32px_rgba(0,0,0,0.12)] backdrop-blur-xl backdrop-saturate-150">
        <NotificationsWorkspace />
      </div>
    </div>
  );
}
