import Link from "next/link";

export default function NotFound() {
  return (
    <main className="graph-paper grid min-h-screen place-items-center px-6">
      <div className="max-w-md text-center">
        <div className="readout text-[96px] font-extrabold text-base">404</div>
        <h1 className="mt-2 text-[22px] font-bold tracking-[-0.015em]">Page not found</h1>
        <p className="mt-2 text-slate">The link may be out of date, or the item was deleted.</p>
        <Link href="/" className="pressable mt-6 inline-flex rounded-lg bg-lagoon px-4 py-2 text-[14px] font-semibold text-white hover:bg-lagoon-ink">
          Go to your projects
        </Link>
      </div>
    </main>
  );
}
