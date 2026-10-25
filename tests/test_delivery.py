import asyncio
import hashlib
import io
import json
import signal
import smtplib
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from docket import main
from docket import store as store_module
from docket.mail import InboxCursor, MailItem, PollResult, UncertainSend
from docket.models import Bundle, Decision, InboundEmail, Reply
from docket.store import Store


class SimulatedCrash(BaseException):
    pass


class FakeMail:
    def __init__(self, *, send_error=None, accepted=False):
        self.send_error = send_error
        self.accepted = accepted
        self.sent = []
        self.looked_up = []

    def send(self, raw):
        self.sent.append(raw)
        if self.send_error:
            raise self.send_error
        self.accepted = True

    def find_sent(self, message_id):
        self.looked_up.append(message_id)
        return self.accepted


def inbound():
    return InboundEmail(
        message_id="<request@example.test>",
        sender="requester@example.test",
        text="Please send one Other Document from M12205.",
    )


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    store.enqueue("job", inbound())
    path = tmp_path / "reply.eml"
    path.write_bytes(b"the exact prepared MIME bytes")
    store.prepare_replies("job", [(1, "<reply@example.test>", path)])
    yield store, path
    store.close()


def row(store):
    return dict(store.db.execute("SELECT * FROM replies WHERE job_id='job'").fetchone())


async def test_uncertain_delivery_is_held_then_reconciled_without_resend(prepared):
    store, _ = prepared
    client = FakeMail(send_error=UncertainSend("lost during DATA"))
    await main.deliver(store, client)
    await main.deliver(store, client)
    assert len(client.sent) == 1
    assert row(store)["status"] == "uncertain"
    client.accepted = True
    await main.deliver(store, client)
    assert len(client.looked_up) == 1
    store.db.execute("UPDATE replies SET available_at=''")
    await main.deliver(store, client)
    assert len(client.sent) == 1
    assert store.summary()["jobs"] == {"sent": 1}


async def test_crash_after_acceptance_recovers_without_duplicate(prepared, tmp_path, monkeypatch):
    store, path = prepared
    client = FakeMail()
    original = store.set_reply

    def crash(identity, part, status, error=None):
        if status == "sent":
            raise SimulatedCrash()
        original(identity, part, status, error)

    monkeypatch.setattr(store, "set_reply", crash)
    with pytest.raises(SimulatedCrash):
        await main.deliver(store, client)
    assert row(store)["status"] == "sending"
    restarted = Store(tmp_path / "state.sqlite")
    try:
        restarted.recover()
        await main.deliver(restarted, client)
        assert client.sent == [path.read_bytes()]
        assert restarted.summary()["jobs"] == {"sent": 1}
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "error,retryable",
    [
        (smtplib.SMTPDataError(451, b"temporary"), True),
        (smtplib.SMTPDataError(552, b"too large"), False),
        (smtplib.SMTPServerDisconnected("before DATA"), True),
    ],
)
async def test_known_failure_has_at_most_one_retry(prepared, error, retryable):
    store, _ = prepared
    client = FakeMail(send_error=error)
    await main.deliver(store, client)
    assert row(store)["status"] == ("ready" if retryable else "failed")
    for _ in range(3):
        store.db.execute("UPDATE replies SET available_at=''")
        await main.deliver(store, client)
    assert len(client.sent) == (2 if retryable else 1)
    assert row(store)["status"] == "failed"


@pytest.mark.parametrize("prior_attempts", [0, 1])
async def test_auth_failure_holds_exact_reply_and_recovers_without_using_retry(
    prepared, monkeypatch, prior_attempts
):
    store, path = prepared
    now = datetime(2026, 10, 25, tzinfo=UTC)
    monkeypatch.setattr(store_module, "utc_now", lambda: now)
    store.db.execute("UPDATE replies SET attempts=?", (prior_attempts,))
    raw = path.read_bytes()
    message_id = row(store)["message_id"]
    client = FakeMail(send_error=smtplib.SMTPAuthenticationError(535, b"Invalid credentials"))

    await main.deliver(store, client)
    held = row(store)
    assert held["status"] == "ready"
    assert held["attempts"] == prior_attempts
    assert datetime.fromisoformat(held["available_at"]) == now + timedelta(seconds=300)
    assert path.read_bytes() == raw

    now += timedelta(seconds=299)
    client.send_error = None
    await main.deliver(store, client)
    assert client.sent == [raw]
    now += timedelta(seconds=1)
    await main.deliver(store, client)
    assert client.sent == [raw, raw]
    assert row(store)["message_id"] == message_id
    assert row(store)["attempts"] == prior_attempts + 1
    assert store.summary()["jobs"] == {"sent": 1}


def set_processor(monkeypatch, processor):
    monkeypatch.setitem(sys.modules, "docket.agent", SimpleNamespace(process_request=processor))


async def test_prepared_reply_prevents_another_model_call(prepared, tmp_path, monkeypatch):
    store, _ = prepared

    async def forbidden(*args, **kwargs):
        raise AssertionError("Model was called after preparing a reply")

    set_processor(monkeypatch, forbidden)
    job = dict(store.db.execute("SELECT * FROM jobs WHERE id='job'").fetchone())
    await main.handle_job(store, tmp_path, job, "owner@gmail.com", "owner+docket@gmail.com")
    assert row(store)["status"] == "ready"


async def test_third_job_attempt_skips_model_and_prepares_visible_failure(tmp_path, monkeypatch):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("The restart limit must prevent another model call")

    set_processor(monkeypatch, forbidden)
    store = Store(tmp_path / "state.sqlite")
    try:
        store.enqueue("job", inbound())
        for _ in range(2):
            store.next_job()
            store.recover()
        job = store.next_job()
        assert job["attempts"] == 3
        await main.handle_job(store, tmp_path, job, "owner@gmail.com", "owner+docket@gmail.com")
        assert calls == []
        assert row(store)["status"] == "ready"
        message = BytesParser(policy=policy.default).parsebytes(
            Path(row(store)["mime_path"]).read_bytes()
        )
        assert message["To"] == inbound().sender
        assert "could not be completed" in message["Subject"]
        assert "No files are attached" in message.get_body(preferencelist=("plain",)).get_content()
        assert list(message.iter_attachments()) == []
    finally:
        store.close()


async def test_acknowledgement_creates_no_email(tmp_path, monkeypatch):
    async def acknowledge(*args, **kwargs):
        return SimpleNamespace(
            decision=Decision(action="no_reply", reason="acknowledgement"),
            reply=None,
            bundles=[],
            matter=None,
        )

    set_processor(monkeypatch, acknowledge)
    store = Store(tmp_path / "state.sqlite")
    try:
        store.enqueue("job", inbound().model_copy(update={"text": "Thanks!"}))
        await main.handle_job(
            store, tmp_path, store.next_job(), "owner@gmail.com", "owner+docket@gmail.com"
        )
        assert store.summary()["jobs"] == {"ignored": 1}
        assert store.pending_replies() == []
    finally:
        store.close()


async def test_cursor_does_not_advance_when_job_persistence_fails(tmp_path, monkeypatch):
    store = Store(tmp_path / "state.sqlite")
    client = FakeMail()
    client.poll = lambda cursor: PollResult(InboxCursor(42, 10), [MailItem("job", 10, inbound())])

    def failed_enqueue(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(store, "enqueue", failed_enqueue)
    try:
        with pytest.raises(sqlite3.OperationalError):
            await main.poll_mail(store, client, tmp_path)
        assert store.get_setting("inbox_cursor") is None
        assert not (tmp_path / "heartbeat.json").exists()
    finally:
        store.close()


@pytest.mark.parametrize("agent_address", [None, "owner@gmail.com", "OWNER@GMAIL.COM"])
async def test_worker_requires_explicit_distinct_alias_before_mailbox_access(
    tmp_path, monkeypatch, agent_address
):
    for key, value in {
        "OPENAI_API_KEY": "test-not-sent",
        "GMAIL_ADDRESS": "owner@gmail.com",
        "GMAIL_APP_PASSWORD": "test-not-sent",
        "DATA_DIR": str(tmp_path / "data"),
    }.items():
        monkeypatch.setenv(key, value)
    if agent_address is None:
        monkeypatch.delenv("AGENT_ADDRESS", raising=False)
    else:
        monkeypatch.setenv("AGENT_ADDRESS", agent_address)
    accessed = []

    def forbidden_client(*args, **kwargs):
        accessed.append(args)
        raise AssertionError("Invalid alias configuration must not access the mailbox")

    monkeypatch.setattr(main, "GmailClient", forbidden_client)
    with pytest.raises(SystemExit, match="AGENT_ADDRESS.*distinct"):
        await main.run_worker()
    assert accessed == []
    assert not (tmp_path / "data").exists()


async def test_worker_persists_zip_before_delivery_and_deduplicates_restart(tmp_path, monkeypatch):
    for key, value in {
        "OPENAI_API_KEY": "test-not-sent",
        "GMAIL_ADDRESS": "owner@gmail.com",
        "GMAIL_APP_PASSWORD": "test-not-sent",
        "AGENT_ADDRESS": "owner+docket@gmail.com",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MAIL_START_AFTER", raising=False)
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, fn: handlers.update({sig: fn}))
    sent = []
    calls = []
    cursors = []

    class WorkerMail(FakeMail):
        def __init__(self, *args, **kwargs):
            super().__init__()

        def poll(self, cursor):
            cursors.append(cursor)
            loop.call_soon_threadsafe(handlers[signal.SIGTERM])
            return PollResult(
                InboxCursor(42, 10), [MailItem("gmail:owner@gmail.com:message:123", 10, inbound())]
            )

        def send(self, raw):
            with sqlite3.connect(tmp_path / "state.sqlite") as db:
                mime_path, status = db.execute("SELECT mime_path,status FROM replies").fetchone()
            assert status == "sending"
            assert Path(mime_path).read_bytes() == raw
            sent.append(raw)

    async def process(request, directory, *, thread_matter):
        heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
        assert heartbeat["at"] and heartbeat["last_poll"]
        calls.append(request)
        path = directory / "documents.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr("one.pdf", b"%PDF-1.4 original bytes")
        payload = path.read_bytes()
        return SimpleNamespace(
            decision=Decision(action="deliver", reason="completed", matter="M12205"),
            reply=Reply(subject="M12205 documents", text="One original attached."),
            bundles=[
                Bundle(
                    path=path,
                    size=len(payload),
                    file_count=1,
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            ],
            matter="M12205",
        )

    monkeypatch.setattr(main, "GmailClient", WorkerMail)
    set_processor(monkeypatch, process)
    await main.run_worker()
    await main.run_worker()
    assert len(calls) == len(sent) == 1
    assert cursors == [None, InboxCursor(42, 10)]
    reply = BytesParser(policy=policy.default).parsebytes(sent[0])
    assert reply["To"] == inbound().sender
    assert reply["From"] == "owner@gmail.com"
    assert reply["Reply-To"] == "owner+docket@gmail.com"
    assert len(list(reply.iter_attachments())) == 1
    assert next(reply.iter_attachments()).get_content_type() == "application/zip"
    with ZipFile(io.BytesIO(next(reply.iter_attachments()).get_payload(decode=True))) as archive:
        assert archive.testzip() is None
        assert archive.read("one.pdf") == b"%PDF-1.4 original bytes"


def test_help_does_not_require_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["docket", "--help"])
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 0
    assert "worker" in capsys.readouterr().out
