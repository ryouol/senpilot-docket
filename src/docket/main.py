import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import signal
import smtplib
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from imapclient import IMAPClient

from .files import atomic_write
from .mail import (
    GmailClient,
    InboxCursor,
    UncertainSend,
    build_reply,
    ignore_reason,
    serialize_message,
)
from .models import Bundle, InboundEmail, Reply, utc_now
from .store import Store

log = logging.getLogger(__name__)


def job_directory(directory: Path, identity: str) -> Path:
    path = directory / "jobs" / hashlib.sha256(identity.encode()).hexdigest()[:32]
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_reply(
    store: Store,
    identity: str,
    inbound: InboundEmail,
    reply: Reply,
    bundles: list[Bundle],
    directory: Path,
    address: str,
    agent_address: str,
) -> None:
    if store.has_replies(identity):
        return
    if sum(bundle.file_count for bundle in bundles) > 10:
        raise ValueError("The reply exceeds the ten-file limit")
    if any(not bundle.path.resolve().is_relative_to(directory.resolve()) for bundle in bundles):
        raise ValueError("Attachment is outside this request's directory")
    parts = [[bundle] for bundle in bundles] or [[]]
    records = []
    for part, attachments in enumerate(parts, 1):
        message_reply = reply
        if len(parts) > 1:
            message_reply = Reply(
                subject=f"{reply.subject} — part {part}/{len(parts)}",
                text=f"Archive part {part} of {len(parts)}. Each ZIP opens independently.\n\n{reply.text}",
            )
        digest = hashlib.sha256(f"{identity}\0{part}".encode()).hexdigest()
        message_id = f"<docket.{digest}@{address.rsplit('@', 1)[-1]}>"
        message = build_reply(
            inbound,
            message_reply,
            attachments,
            address,
            message_id=message_id,
            reply_to=agent_address,
        )
        path = directory / f"reply-{part:02}.eml"
        atomic_write(path, serialize_message(message))
        records.append((part, message_id, path))
    store.prepare_replies(identity, records)


async def handle_job(
    store: Store, directory: Path, job: dict, address: str, agent_address: str
) -> None:
    from .agent import process_request

    identity = job["id"]
    if store.has_replies(identity):
        return
    inbound = InboundEmail.model_validate_json(job["inbound"])
    reason = ignore_reason(inbound, address) or ignore_reason(inbound, agent_address)
    if reason:
        store.set_job(identity, "ignored", error=reason)
        return
    work_dir = job_directory(directory, identity)
    try:
        if job["attempts"] > 2:
            raise ValueError("Request restart limit reached")
        outcome = await process_request(
            inbound, work_dir, thread_matter=store.thread_matter(inbound)
        )
        atomic_write(work_dir / "decision.json", outcome.decision.model_dump_json().encode())
        if outcome.decision.action == "no_reply":
            store.set_job(identity, "ignored")
            return
        if outcome.reply is None:
            raise ValueError("The agent returned no reply for an actionable request")
        if outcome.decision.action == "deliver" and not outcome.bundles:
            raise ValueError("The agent returned delivery without verified archives")
        if outcome.decision.action != "deliver" and outcome.bundles:
            raise ValueError("The agent returned archives without a delivery decision")
        store.set_job(identity, "running", matter=outcome.matter)
        prepare_reply(
            store,
            identity,
            inbound,
            outcome.reply,
            outcome.bundles,
            work_dir,
            address,
            agent_address,
        )
    except Exception as exc:  # noqa: BLE001 - a failed request must not stop the mailbox worker.
        log.error("Request preparation failed: %s", type(exc).__name__)
        store.set_job(identity, "failed", error=type(exc).__name__)
        prepare_reply(
            store,
            identity,
            inbound,
            Reply(
                subject="Docket — request could not be completed",
                text="I could not safely complete this request. No files are attached. Please try again later.",
            ),
            [],
            work_dir,
            address,
            agent_address,
        )


async def deliver(store: Store, client: GmailClient) -> None:
    for reply in store.pending_replies():
        identity, part = reply["job_id"], reply["part"]
        try:
            if reply["status"] == "uncertain":
                store.defer_reconciliation(identity, part)
                if await asyncio.to_thread(client.find_sent, reply["message_id"]):
                    store.set_reply(identity, part, "sent")
                continue
            try:
                raw = Path(reply["mime_path"]).read_bytes()
            except OSError:
                store.set_reply(identity, part, "failed", "Prepared email file is unavailable")
                continue
            if reply["attempts"] >= 2:
                store.set_reply(identity, part, "failed", "Delivery retry limit reached")
                continue
            store.sending(identity, part)
            try:
                await asyncio.to_thread(client.send, raw)
            except UncertainSend:
                store.set_reply(identity, part, "uncertain", "SMTP acceptance is unknown")
            except smtplib.SMTPAuthenticationError:
                log.warning("Mailbox authentication failed; holding prepared replies")
                store.defer_reply(
                    identity,
                    part,
                    "Mailbox authentication unavailable",
                    delay=300,
                    count_attempt=False,
                )
            except smtplib.SMTPResponseException as exc:
                if 400 <= exc.smtp_code < 500:
                    store.defer_reply(identity, part, f"SMTP response {exc.smtp_code}")
                else:
                    store.set_reply(identity, part, "failed", f"SMTP response {exc.smtp_code}")
            except smtplib.SMTPRecipientsRefused as exc:
                if exc.recipients and all(400 <= code < 500 for code, _ in exc.recipients.values()):
                    store.defer_reply(identity, part, "Recipient temporarily refused")
                else:
                    store.set_reply(identity, part, "failed", "Recipient refused")
            except ValueError:
                store.set_reply(identity, part, "failed", "Prepared email failed validation")
            except (OSError, smtplib.SMTPException):
                store.defer_reply(identity, part, "Connection failed before acceptance")
            else:
                store.set_reply(identity, part, "sent")
        except (OSError, ValueError, smtplib.SMTPException, IMAPClient.Error) as exc:
            log.warning("Email delivery or reconciliation failed: %s", type(exc).__name__)


def bind_mailbox(store: Store, address: str, agent_address: str) -> datetime:
    identity = json.dumps([address.casefold(), agent_address.casefold()])
    previous = store.get_setting("mailbox_identity")
    if previous and previous != identity:
        raise ValueError("This data directory belongs to another mailbox/alias")
    saved = store.get_setting("started_at")
    configured = os.environ.get("MAIL_START_AFTER")
    if saved and configured and datetime.fromisoformat(saved) != datetime.fromisoformat(configured):
        raise ValueError("MAIL_START_AFTER cannot replace the saved mailbox cutoff")
    started = saved or configured or utc_now().isoformat()
    cutoff = datetime.fromisoformat(started)
    if cutoff.tzinfo is None:
        raise ValueError("MAIL_START_AFTER must include a timezone")
    store.set_setting("mailbox_identity", identity)
    store.set_setting("started_at", started)
    return cutoff


def heartbeat(store: Store, directory: Path) -> None:
    atomic_write(
        directory / "heartbeat.json",
        json.dumps({"at": utc_now().isoformat(), **store.summary()}).encode(),
    )


async def poll_mail(store: Store, client: GmailClient, directory: Path) -> None:
    saved = store.get_setting("inbox_cursor")
    cursor = InboxCursor(**json.loads(saved)) if saved else None
    polled = await asyncio.to_thread(client.poll, cursor)
    for item in polled.items:
        if item.email is not None:
            store.enqueue(item.identity, item.email)
        else:
            log.warning("Skipped unreadable inbound message")
    store.set_setting("inbox_cursor", json.dumps(asdict(polled.cursor)))
    store.set_setting("last_poll", utc_now().isoformat())
    heartbeat(store, directory)


def require_model_key() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running the agent")


async def run_worker() -> None:
    require_model_key()
    address = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    agent_address = os.environ.get("AGENT_ADDRESS", "")
    if not address or not password:
        raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD")
    if not agent_address or agent_address.casefold() == address.casefold():
        raise SystemExit("Set AGENT_ADDRESS to a dedicated alias, distinct from GMAIL_ADDRESS")
    directory = Path(os.environ.get("DATA_DIR", "data")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    interval = max(10, int(os.environ.get("POLL_SECONDS", "30")))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker owns this data directory") from None
        store = Store(directory / "state.sqlite")
        try:
            store.recover()
            cutoff = bind_mailbox(store, address, agent_address)
            client = GmailClient(
                address, password, start_after=cutoff, recipient_filter=agent_address
            )
            log.info("Worker started; polling every %ss", interval)
            while not stop.is_set():
                try:
                    await poll_mail(store, client, directory)
                except (OSError, ValueError, IMAPClient.Error) as exc:
                    log.warning("Inbox poll failed: %s", type(exc).__name__)
                await deliver(store, client)
                job = store.next_job()
                if job:
                    await handle_job(store, directory, job, address, agent_address)
                    await deliver(store, client)
                heartbeat(store, directory)
                if not job:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=interval)
                    except TimeoutError:
                        pass
        finally:
            store.close()
            log.info("Worker stopped")


async def run_local(request: str, output: Path) -> None:
    from .agent import process_request

    require_model_key()
    identity = uuid.uuid4().hex
    directory = output.resolve() / identity[:12]
    directory.mkdir(parents=True)
    inbound = InboundEmail(
        message_id=f"<{identity}@local.invalid>", sender="requester@local.invalid", text=request
    )
    outcome = await process_request(inbound, directory)
    print(outcome.decision.model_dump_json(indent=2))
    if outcome.reply:
        print(f"\n{outcome.reply.subject}\n{outcome.reply.text}")
    for bundle in outcome.bundles:
        print(f"Attachment: {bundle.path}")
    print(f"Artifacts: {directory}\nNo email sent.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Email-operated AI agent for UARB documents")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker", help="Poll the configured Gmail alias")
    local = commands.add_parser("run", help="Run one request locally without sending email")
    local.add_argument("request")
    local.add_argument("--output", type=Path, default=Path("artifacts"))
    status = commands.add_parser("status", help="Read mailbox and delivery status")
    status.add_argument("--data", type=Path)
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "worker":
        asyncio.run(run_worker())
    elif args.command == "run":
        asyncio.run(run_local(args.request, args.output))
    else:
        store = Store((args.data or Path(os.environ.get("DATA_DIR", "data"))) / "state.sqlite")
        try:
            print(json.dumps(store.summary(), indent=2))
        finally:
            store.close()


if __name__ == "__main__":
    main()
