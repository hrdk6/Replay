"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useShell } from "@/components/app-shell";
import { EmptyState, Loading } from "@/components/ui";

export default function Home() {
  const { projects } = useShell();
  const router = useRouter();
  const first = projects[0];

  useEffect(() => {
    if (first) router.replace(first.trace_count ? `/p/${first.id}/traces` : `/p/${first.id}/quickstart`);
  }, [first, router]);

  if (!first) return <EmptyState title="No projects yet">Create a project in organization settings to start capturing traces.</EmptyState>;
  return <Loading />;
}
