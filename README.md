# Docket

An email-operated AI agent that retrieves public UARB filings and replies with ZIP attachments, matter metadata and document counts.

One OpenAI Agents SDK agent uses **GPT-6 Luna with low reasoning effort** to inspect, retrieve and choose a typed completion. Python verifies files and prepares email.

## Try it

Agent address: **royluo05+docket@gmail.com**.

> Please send three Other Documents from M12205.

Put the alias in **To**; send from another mailbox. Types: Exhibits, Key Documents, Other Documents, Transcripts and Recordings.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/ryouol/senpilot-docket.git
cd senpilot-docket
uv sync --frozen --extra dev
uv run playwright install chromium
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`. This runs the real model and portal without sending email:

```sh
uv run docket run "Send three Other Documents from M12205."
tail -n 20 artifacts/*/actions.jsonl
```

For email mode, also set `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and `AGENT_ADDRESS`:

```sh
uv run docket worker
uv run docket status
```

Keep one worker per mailbox and preserve its data directory. Logs: worker output and `data/jobs/`.

## Watch a request

Open [OpenAI Logs → Agents SDK](https://platform.openai.com/logs?api=traces) in the API key's project. New runs appear as **Docket email agent**; match the trace ID printed in the worker logs. Spans upload in the background, so refresh after a few seconds. Email text and tool payloads are excluded from traces. Set `OPENAI_AGENTS_DISABLE_TRACING=1` to disable export.

## Checks

```sh
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

Verified October 25: 145 offline tests pass. Live model/portal retrieval, empty-category and missing-matter checks passed. A ten-file ZIP reached an independent mailbox with matching hashes; a worker restart did not resend it.

## Limits

One matter/category per request; up to ten public originals. Large files may be omitted. Portal counters describe listed entries, not independently reconciled physical files. Delivery uncertainty is held for reconciliation, with no exactly-once guarantee.

[Planning and testing](docs/planning.md) · [Architecture](docs/architecture.md) · [Tradeoffs](docs/tradeoffs.md)
