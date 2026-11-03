import { redirect } from "next/navigation";

/**
 * `/tasks/<id>` is the shape people paste from links/bookmarks. The task board
 * opens its detail drawer from `?id=`, so anything path-shaped bounced to the
 * Next.js 404 page instead of the task. Keep one canonical URL.
 */
export default async function TaskDetailRedirectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  redirect(`/tasks?id=${encodeURIComponent(id)}`);
}
