import type { Metadata } from "next";
import { Big_Shoulders, Hanken_Grotesk, JetBrains_Mono } from "next/font/google";
import "./globals.css";

// UI text: a warm, legible grotesk with tabular figures for data.
const hanken = Hanken_Grotesk({ subsets: ["latin"], variable: "--font-ui" });
// Readouts and verdicts: instrument-panel signage, used only for big numbers and verdict words.
const shoulders = Big_Shoulders({ subsets: ["latin"], variable: "--font-shoulders", axes: ["opsz"] });
// Payloads, ids and code only.
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-code" });

export const metadata: Metadata = {
  title: { default: "Replay", template: "%s · Replay" },
  description: "Test model, prompt and retrieval changes against your real production traces before you ship them.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className={`${hanken.variable} ${shoulders.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
