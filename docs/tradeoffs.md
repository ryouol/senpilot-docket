# Tradeoffs

Docket handles one matter and one category per request. The model decides whether to inspect, retrieve, clarify or report an observed failure. Tool code restricts what those decisions can do.

## Decisions and costs

| Choice | Benefit | Cost or limitation |
| --- | --- | --- |
| Two application tools and typed completion | Keeps model decisions visible while source and delivery controls stay in Python | The model cannot freely browse or investigate arbitrary cases |
| OpenAI Agents SDK | Supplies the loop, function tools and structured output | Adds SDK behavior that needs testing; request correctness still belongs to this application |
| Deterministic navigation | Checks active matter, row and filenames consistently | Changes to the FileMaker interface can break selectors |
| Polling Gmail | Works without a webhook server or another mail provider | Requests wait for the next poll; the alias is part of a personal mailbox |
| SQLite plus saved MIME | Prevents ordinary duplicate jobs and preserves pending replies | One machine and one worker; unresolved delivery needs inspection |
| Existing GCP deployment | Keeps the email address usable while the laptop is offline | Hosting and updates remain operational work beyond the core agent |

The Gmail alias filters which messages the application processes. It does not restrict the underlying app password to that alias. Responses use the original sender; the model cannot redirect them.

## Counts and source identity

The five counters are portal entries, including confidential entries. Key Documents also appear under Other Documents, so the reported total excludes that known overlap. An entry can contain several physical files. The total is therefore not an independently reconciled unique-file count.

The attachment count describes original files inside the prepared ZIPs, excluding manifests. Selection follows the portal's displayed order; it does not promise the newest filings. Omission reports cover encountered entries unless the whole category was scanned.

Different source records can serve identical bytes. Docket preserves their identities and discloses matching hashes. Filename and row checks address source selection; hashes establish that the bytes survived packaging unchanged. File signatures reject empty payloads and obvious format mismatches, but do not fully parse or validate PDF, Office or audio contents.

## Bounds and failure behavior

| Bound | Limit |
| --- | ---: |
| Original files per request | 10 |
| Source file and each ZIP | 18,000,000 bytes |
| Streamed bytes per portal attempt | 60,000,000 |
| Portal attempt | 180 seconds |
| Serialized email, including encoding | 25,000,000 bytes |
| Model turns per run | 6 |
| Each serialized model input or tool observation | 8,000 UTF-8 bytes |

Explicitly restricted entries are skipped. Some transcript/recording rows have no access label; these can use the normal anonymous download path, retain the blank label in manifests, and disclose it in the reply. Unlabelled rows in other categories remain skipped. Large recordings may be skipped. Original files are not transcoded. ZIPs can be split across separate replies when needed; each opens independently. Verified files from a partially failed row are retained, and failures are disclosed.

One eligible transient retry is shared across model and portal operations. SMTP has its own delivery handling, allowing a second attempt after a known retryable rejection or pre-acceptance failure. If acceptance is uncertain, the worker checks Sent Mail. A missing Sent result does not prove non-delivery. There is no exactly-once email guarantee.

In-flight model context is not durably checkpointed. Preparation may repeat after restart, with at most two attempts before a failure reply. Persisted identities and exact MIME protect prepared replies from ordinary replay. Authentication failure holds a reply for five minutes without consuming its send retry. A disk reserve prevents starting retrieval without enough working space; artifacts still need manual retention cleanup.

## With another day

Priorities would be real-model evaluations across the five categories, additional fixtures for virtual-grid changes, explicit artifact retention and better diagnostics for uncertain delivery. Full underlying-file reconciliation would need separate work and a clearer counting contract.
