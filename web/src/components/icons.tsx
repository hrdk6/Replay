import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 18, ...props }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    ...props,
  };
}

/** Spans on a timeline: what a trace looks like. */
export const TracesIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 6h9M7 12h11M5 18h6" />
    <circle cx="16.5" cy="6" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="13.5" cy="18" r="1.2" fill="currentColor" stroke="none" />
  </svg>
);

/** A frozen stack of recordings. */
export const DatasetsIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <ellipse cx="12" cy="6" rx="7" ry="2.6" />
    <path d="M5 6v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6M5 12v6c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-6" />
  </svg>
);

/** A proposed change branching off. */
export const CandidatesIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="6" cy="5.5" r="2" />
    <circle cx="6" cy="18.5" r="2" />
    <circle cx="18" cy="9" r="2" />
    <path d="M6 7.5v9M18 11c0 4-6 3.2-11.2 6.2" />
  </svg>
);

/** Two takes of the same recording, compared. */
export const ExperimentsIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 8.5 9 5v7z" />
    <path d="M12 15.5 17 12v7z" />
    <path d="M3.5 20h17" />
  </svg>
);

/** Balance scale. */
export const JudgesIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 4v16M7 20h10M5 7h14" />
    <path d="M5 7 2.5 13a2.6 2.6 0 0 0 5 0zM19 7l-2.5 6a2.6 2.6 0 0 0 5 0z" />
  </svg>
);

/** A label being applied by hand. */
export const LabelingIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3.5 12.2V4.5a1 1 0 0 1 1-1h7.7l8.3 8.3-8.7 8.7z" />
    <circle cx="8" cy="8" r="1.3" />
  </svg>
);

export const QuickstartIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13 3 5 13.5h6L10 21l8-10.5h-6z" />
  </svg>
);

export const SettingsIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
    <circle cx="16" cy="7" r="2" />
    <circle cx="10" cy="17" r="2" />
  </svg>
);

export const OrgIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 20V8l6-4 6 4v12M16 11h4v9M4 20h17M8 11h4M8 15h4" />
  </svg>
);

export const ReplayIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4.5h4.5" />
  </svg>
);

export const CheckIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </svg>
);

export const AlertIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 4 2.8 19.5h18.4zM12 10v4.2" />
    <circle cx="12" cy="16.8" r="0.6" fill="currentColor" />
  </svg>
);

export const InfoIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 11v5.5" />
    <circle cx="12" cy="7.8" r="0.6" fill="currentColor" />
  </svg>
);

export const CopyIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="8.5" y="8.5" width="11" height="11" rx="2" />
    <path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5" />
  </svg>
);

export const MenuIcon = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </svg>
);

export function Logo({ size = 26 }: { size?: number }) {
  // Two offset play-heads: the recording and its replay.
  return (
    <svg width={size} height={size} viewBox="0 0 26 26" aria-hidden>
      <rect width="26" height="26" rx="7" fill="var(--color-lagoon)" />
      <path d="M7 7.5 14.5 13 7 18.5z" fill="#ffffff" opacity="0.45" />
      <path d="M11 7.5 18.5 13 11 18.5z" fill="#ffffff" />
    </svg>
  );
}
