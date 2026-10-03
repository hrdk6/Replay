import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "./icons";

export function LegalPage({ title, updated, children }: { title: string; updated: string; children: ReactNode }) {
  return (
    <main className="mx-auto max-w-[720px] px-6 py-12">
      <Link href="/login" className="inline-flex items-center gap-2 text-[14px] font-bold tracking-[-0.01em] text-ink">
        <Logo size={22} /> Replay
      </Link>
      <h1 className="mt-8 text-[30px] font-bold tracking-[-0.02em]">{title}</h1>
      <p className="mt-1 text-[13px] text-slate">Last updated {updated}</p>
      <div role="note" className="mt-6 rounded-lg border border-unsure-bright/40 bg-unsure-tint px-4 py-3 text-[13.5px] text-[#6b4204]">
        Draft. This document has not been reviewed by a lawyer and is not yet in effect. It describes how the software behaves today so that review can start from facts.
      </div>
      <article className="prose-legal mt-8 space-y-5 text-[14.5px] leading-[1.7] [&_a]:font-medium [&_a]:text-lagoon-ink [&_h2]:mt-8 [&_h2]:text-[17px] [&_h2]:font-bold [&_li]:ml-5 [&_li]:list-disc">
        {children}
      </article>
    </main>
  );
}
