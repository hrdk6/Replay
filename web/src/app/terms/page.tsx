import type { Metadata } from "next";
import { LegalPage } from "@/components/legal";

export const metadata: Metadata = { title: "Terms of service (draft)" };

export default function TermsPage() {
  return (
    <LegalPage title="Terms of service" updated="October 3, 2026">
      <h2>The service</h2>
      <p>Replay stores traces from your applications and replays them against changes you configure, using LLM provider accounts you connect. It is offered as a beta: features, limits and pricing may change.</p>
      <h2>Your data and your responsibilities</h2>
      <ul>
        <li>You keep all rights to the data you send. You grant us the rights needed to store, process and display it to provide the service.</li>
        <li>You are responsible for having the right to send that data to us, including any personal data in prompts or outputs, and for configuring redaction and retention appropriately.</li>
        <li>LLM calls made during experiments use your provider keys and are billed to you by those providers under their terms. Budgets in Replay limit spend but cannot guarantee an exact amount.</li>
        <li>Do not use Replay to store data you are not permitted to process outside your systems, or to attack, overload or probe the service.</li>
      </ul>
      <h2>Statistical results</h2>
      <p>Verdicts are statistical estimates based on the traces, judges and settings you choose. They can be wrong, especially with uncalibrated judges, small datasets or high divergence. They are decision support, not a guarantee of quality.</p>
      <h2>Availability and liability</h2>
      <p>The beta is provided as is, without warranty. Limitation of liability, governing law and dispute terms must be written with legal counsel before launch.</p>
      <h2>Ending use</h2>
      <p>You can export your data and delete your organization at any time. We may suspend accounts that violate these terms.</p>
    </LegalPage>
  );
}
