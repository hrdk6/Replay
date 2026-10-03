import type { Metadata } from "next";
import { LegalPage } from "@/components/legal";

export const metadata: Metadata = { title: "Privacy policy (draft)" };

export default function PrivacyPage() {
  return (
    <LegalPage title="Privacy policy" updated="October 3, 2026">
      <h2>What we collect</h2>
      <ul>
        <li>Your GitHub profile basics when you sign in: user id, username, name, avatar and primary verified email. We request no repository access.</li>
        <li>Traces your application sends with your API key: prompts, model outputs, tool calls and their results, timings, token counts and any metadata you attach.</li>
        <li>Configuration you create: projects, datasets, candidates, judges, experiments, human labels and settings.</li>
        <li>Operational data: an audit log of sensitive actions (with IP address), usage counters, and error reports with request bodies and secrets removed.</li>
      </ul>
      <h2>How trace data is protected</h2>
      <ul>
        <li>Pattern-based redaction of emails, phone numbers, card numbers, secrets and custom patterns runs before data is stored, when enabled for a project (on by default). It can miss things; do not rely on it as your only control.</li>
        <li>Each organization&apos;s data is isolated in the application and by database row-level security.</li>
        <li>Data is encrypted in transit (TLS) and at rest by the database and object-storage providers.</li>
        <li>Your LLM provider keys are encrypted with a per-key data key and are never displayed or logged.</li>
      </ul>
      <h2>How we use it</h2>
      <p>Only to provide the service you configure: storing and showing traces, replaying them against your candidate changes using your own LLM provider keys, judging the results, and computing reports. We do not train models on your data and do not sell it.</p>
      <h2>Sub-processors</h2>
      <p>Hosting, managed database and object storage provider(s), error tracking (if enabled), and GitHub for sign-in. When you run experiments, your prompts and recorded data are sent to the LLM providers whose keys you configured, under your agreements with them. The final list depends on the hosting choice and must be completed before launch.</p>
      <h2>Retention and deletion</h2>
      <ul>
        <li>Traces are deleted after each project&apos;s retention period (30 days by default, configurable).</li>
        <li>Datasets are frozen copies kept until you delete them or the project.</li>
        <li>You can delete individual traces, datasets, experiments, projects, your organization and your account at any time, and export all organization data as JSON.</li>
        <li>Backups may retain deleted data for up to the backup retention window of the database provider.</li>
      </ul>
      <h2>Contact</h2>
      <p>Questions or requests: privacy contact address to be added before launch.</p>
    </LegalPage>
  );
}
