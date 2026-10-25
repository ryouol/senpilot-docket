# Planning and testing

Docket accepts one matter and category by email, retrieves up to ten public original files, and returns ZIPs with metadata and counts. Searchable databases and document analysis are outside this implementation.

```mermaid
flowchart LR
    email["Email request"] --> agent["Model chooses tools"]
    agent <--> portal["Inspect or retrieve UARB files"]
    portal --> verify["Verify originals and ZIP"]
    verify --> decision["Model sees result; code validates decision"]
    decision --> reply["Reply with facts and attachments"]
```

## Scope and choices

Every eligible request reaches the model, including incomplete requests and acknowledgements. ZIP manifests record source IDs, sizes and hashes; Python renders verified counters and omissions. Replies go to the original sender.

The job description emphasizes AI-assisted shipping, data ingestion and technical ownership. It specifies no model vendor, SDK or hosting platform.

| Choice | Reason and alternative considered |
| --- | --- |
| Python | Reuses proven browser and mail operations. TypeScript with Vercel AI SDK would require a rewrite or a second runtime. |
| OpenAI Agents SDK | Owns tool dispatch and the model loop. A plain provider SDK would require a custom loop. |
| GPT-6 Luna, low effort | One model handles this bounded task; actual access and retrieval passed live checks. |
| Playwright + HTTPX | The portal needs browser state. HTTPX streams the download URL produced by the portal and enforces byte limits. |
| IMAP/SMTP | Supports polling and ZIP attachments through the configured mailbox. Gmail API or another mail provider adds setup. |
| SQLite and local files | Keeps inbox identities, prepared messages and delivery status together without another service. |
| Existing GCP VM | Keeps the mailbox worker available. Local execution remains available for development; new managed infrastructure is unnecessary. |

## Design correction and review

The first version used rules for ordinary requests and optional AI classification. That left the model outside the main decisions. This version lets the model call tools, receive observations and choose a validated completion.

Claude challenged mandatory inspect-then-retrieve sequencing and recommended optional inspection, existing browser/mail tools and bounded retries. It preferred retaining Anthropic to avoid migration work. The final choice uses OpenAI Agents SDK to provide the loop. A proposed shortcut that inferred the requested category from file availability was rejected: ambiguity should lead to clarification.

## Publication scope

Commits group the final implementation by task. Their timestamps describe publication, not development time: research, autonomous agent work and selectively recycled browser/mail adapters predate them. This history is not evidence of three hours of total effort. The scope stays at one agent, two tools, verified ZIPs and email delivery.

## Verification

Scripted-model tests verify result-dependent tool choices, rejected arguments, evidence-backed completion and the six-turn limit. Adapter tests cover source identity, file limits, ZIP/MIME integrity, inbox cursors and uncertain SMTP delivery. An SDK argument-parsing regression allowed unexpected fields to be dropped; raw tool arguments are now checked before side effects.

Real GPT-6 Luna requests retrieved one and ten Other Documents from M12205. The ten-file ZIP was 10,655,357 bytes. Its source counters were 13 Exhibits, 6 Key Documents, 43 Other Documents and zero Transcripts/Recordings: 56 listed entries excluding the Key Documents overlap. Two pairs of source records served identical bytes and were disclosed.

A live Exhibits request retained one public file under the configured limits, disclosed six confidential entries and oversized downloads, and scanned 12 of 13 entries before exhausting the download budget. Controlled-source tests with the real model passed both zero/positive availability and acknowledgement/new-request cases. These do not replace live portal coverage.

The latest check passed 144 offline tests; eleven opt-in live tests were skipped in that run. Ruff and ShellCheck passed. Separate live Transcripts and M00000 requests verified empty-category and missing-matter behavior. The ten-file ZIP passed integrity, manifest size and every file-hash check. An independent browser view matched the title, dates and all five counters. The GCP candidate sent ten originals to an independent university mailbox; the received ZIP exactly matched the prepared archive and all hashes. Restarting preserved the completed job, MIME and one send attempt, with no duplicate reply.

Additional live checks retrieved a Key Document from M12383 directly without inspection, and a Transcript from M10431. Its Recordings were anonymously accessible but exceeded the file/request budgets, so no audio ZIP was claimed. Review caught a fresh subject-only request misclassified as thanks; explicit reply context corrected it and real-model retesting passed. Unlabelled transcript/recording rows now use the ordinary public download path, while explicit restrictions remain blocked.
