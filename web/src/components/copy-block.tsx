"use client";

import { useState } from "react";
import { CheckIcon, CopyIcon } from "./icons";

export function CopyBlock({ code, label }: { code: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative">
      {label ? <div className="mb-1.5 text-[12.5px] font-medium text-slate">{label}</div> : null}
      <div className="relative overflow-hidden rounded-xl bg-rail ring-1 ring-white/5">
        <pre className="overflow-x-auto p-4 pr-24 font-mono text-[12.5px] leading-relaxed text-[#d9e3f3]">{code}</pre>
        <button
          type="button"
          onClick={async () => {
            await navigator.clipboard.writeText(code);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          }}
          className={`pressable absolute right-2.5 top-2.5 inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] font-semibold ${copied ? "bg-lagoon text-white" : "bg-white/10 text-rail-text hover:bg-white/15 hover:text-white"}`}
        >
          {copied ? <CheckIcon size={13} /> : <CopyIcon size={13} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}
