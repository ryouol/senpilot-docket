import hashlib
import smtplib
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage

import pytest

from docket import mail
from docket.mail import (
    GmailClient,
    InboxCursor,
    InvalidMessage,
    MessageTooLarge,
    UncertainSend,
    build_reply,
    ignore_reason,
    normalize_inbound,
    serialize_message,
)
from docket.models import Bundle, Reply

OWN = "docket@example.com"
START = datetime(2026, 10, 24, 0, 30, tzinfo=UTC)


def raw_message(sender="Roy <roy@example.com>", body="M12205 Other Documents"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = OWN
    message["Message-ID"] = "<request@example.com>"
    message["Subject"] = "Fetch documents"
    message.set_content(body)
    return message


def outgoing():
    inbound = normalize_inbound(raw_message().as_bytes())
    return serialize_message(
        build_reply(
            inbound,
            Reply(subject="Documents", text="Ready"),
            [],
            OWN,
            message_id="<reply@example.com>",
        )
    )


def test_mime_prefers_plain_text_and_excludes_attached_email():
    message = raw_message(body="M12205 — Board orders")
    message.add_alternative("<p>Duplicate HTML</p>", subtype="html")
    message.make_mixed()
    message.add_attachment(raw_message(body="M99999 malicious attachment"))
    result = normalize_inbound(message.as_bytes())
    assert result.sender == "roy@example.com"
    assert result.text == "M12205 — Board orders"


def test_html_only_body_is_decoded_without_script_or_style():
    message = raw_message()
    message.set_content("<p>M12205 &amp; documents</p><script>M99999</script>", subtype="html")
    assert normalize_inbound(message.as_bytes()).text == "M12205 & documents"


def test_html_signature_table_cells_remain_separate_lines():
    message = raw_message()
    message.set_content(
        "<p>Please send one Other Document for M12205.</p>"
        "<table><tr><th>Best,</th><td>Example Person</td>"
        "<td>Email: person@example.test</td></tr></table>",
        subtype="html",
    )
    lines = normalize_inbound(message.as_bytes()).text.splitlines()
    assert [line for line in lines if line] == [
        "Please send one Other Document for M12205.",
        "Best,",
        "Example Person",
        "Email: person@example.test",
    ]


@pytest.mark.parametrize("sender", ["a@example.com, b@example.com", "not-an-address", "a@@b"])
def test_reject_ambiguous_or_invalid_from(sender):
    raw = f"From: {sender}\r\nSubject: test\r\n\r\nM12205".encode()
    with pytest.raises(InvalidMessage):
        normalize_inbound(raw)


def test_duplicate_from_header_is_rejected():
    with pytest.raises(InvalidMessage):
        normalize_inbound(b"From: a@example.com\r\nFrom: b@example.com\r\n\r\nrequest")


def test_missing_message_id_has_deterministic_fallback():
    message = raw_message()
    del message["Message-ID"]
    raw = message.as_bytes()
    assert normalize_inbound(raw).message_id == normalize_inbound(raw).message_id


@pytest.mark.parametrize(
    "header,value,reason",
    [
        ("Auto-Submitted", "auto-replied", "automatic_message"),
        ("Return-Path", "<>", "delivery_report"),
        ("Precedence", "bulk", "mailing_list"),
        ("List-ID", "announcements.example.com", "mailing_list"),
        ("X-Auto-Response-Suppress", "DR, AutoReply", "auto_reply_suppressed"),
    ],
)
def test_reply_loop_guards(header, value, reason):
    message = raw_message()
    message[header] = value
    assert ignore_reason(normalize_inbound(message.as_bytes()), OWN) == reason


def test_normal_request_is_allowed_and_own_mail_is_ignored():
    assert ignore_reason(normalize_inbound(raw_message().as_bytes()), OWN) is None
    assert ignore_reason(normalize_inbound(raw_message(OWN).as_bytes()), OWN) == "own_message"


def test_reply_uses_sender_not_reply_to_and_checks_bundle(tmp_path):
    request = raw_message()
    request["Reply-To"] = "attacker@example.com"
    request["References"] = "<earlier@example.com>"
    path = tmp_path / "documents.zip"
    payload = b"PK\x03\x04archive"
    path.write_bytes(payload)
    bundle = Bundle(
        path=path, size=len(payload), file_count=1, sha256=hashlib.sha256(payload).hexdigest()
    )
    reply = build_reply(
        normalize_inbound(request.as_bytes()),
        Reply(subject="Ready\r\nBcc: attacker@example.com", text="Done"),
        [bundle],
        OWN,
        message_id="<fixed@example.com>",
    )
    assert reply["To"] == "roy@example.com"
    assert reply["Bcc"] is None
    assert reply["Message-ID"] == "<fixed@example.com>"
    assert reply["In-Reply-To"] == "<request@example.com>"
    assert "<earlier@example.com>" in reply["References"]
    assert reply["Auto-Submitted"] == "auto-replied"
    assert next(reply.iter_attachments()).get_payload(decode=True) == payload
    path.write_bytes(b"changed")
    with pytest.raises(InvalidMessage, match="changed"):
        build_reply(
            normalize_inbound(request.as_bytes()),
            Reply(subject="x", text="x"),
            [bundle],
            OWN,
            message_id="<fixed@example.com>",
        )


def test_serialized_limit_includes_base64_and_headers(monkeypatch, tmp_path):
    monkeypatch.setattr(mail, "MAX_MESSAGE_BYTES", 2_000)
    payload = b"x" * 1_500
    path = tmp_path / "documents.zip"
    path.write_bytes(payload)
    bundle = Bundle(
        path=path, size=len(payload), file_count=1, sha256=hashlib.sha256(payload).hexdigest()
    )
    with pytest.raises(MessageTooLarge):
        serialize_message(
            build_reply(
                normalize_inbound(raw_message().as_bytes()),
                Reply(subject="x", text="x"),
                [bundle],
                OWN,
                message_id="<fixed@example.com>",
            )
        )


class FakeIMAP:
    Error = OSError

    def __init__(self, *_args, **_kwargs):
        self.selected = []
        self.searches = []
        self.fetches = []
        self.validity = 42
        self.uids = [9, 10, 11]
        self.data = {
            uid: {
                b"BODY[]": raw_message().as_bytes(),
                b"BODY[HEADER.FIELDS (TO)]": b"To: docket@example.com\r\n\r\n",
                b"X-GM-MSGID": 1000 + uid,
                b"RFC822.SIZE": len(raw_message().as_bytes()),
                b"INTERNALDATE": START + timedelta(minutes=1),
            }
            for uid in self.uids
        }

    def login(self, *_args):
        pass

    def logout(self):
        pass

    def select_folder(self, name, readonly):
        self.selected.append((name, readonly))
        return {b"UIDVALIDITY": self.validity}

    def search(self, criteria):
        self.searches.append(criteria)
        return self.uids

    def fetch(self, uids, fields):
        self.fetches.append(fields)
        return {uid: self.data[uid] for uid in uids}

    def list_folders(self):
        return [((b"\\Sent",), "/", "Localized Sent Folder")]


@pytest.fixture
def imap(monkeypatch):
    instance = FakeIMAP()
    monkeypatch.setattr(mail, "IMAPClient", lambda *args, **kwargs: instance)
    # The transport's cleanup exception alias is supplied by the real library.
    mail.IMAPClient.Error = OSError
    return instance


def test_poll_uses_uid_not_unseen_and_caps_batch(imap):
    result = GmailClient(OWN, "secret", start_after=START, max_messages=1).poll(InboxCursor(42, 9))
    assert result.cursor == InboxCursor(42, 10)
    assert len(result.items) == 1
    assert result.items[0].identity == f"gmail:{OWN}:message:1010"
    assert "UNSEEN" not in imap.searches[0]
    assert imap.searches[0][:2] == ["UID", "10:*"]
    assert imap.selected == [("INBOX", True)]
    assert "RFC822.SIZE" in imap.fetches[0]
    assert "BODY.PEEK[]" not in imap.fetches[0]
    assert imap.fetches[1] == ["BODY.PEEK[]"]


def test_uidvalidity_reset_reconciles_since_boundary_with_stable_identity(imap):
    imap.data[9][b"INTERNALDATE"] = START - timedelta(seconds=1)
    result = GmailClient(OWN, "secret", start_after=START).poll(InboxCursor(999, 9999))
    assert imap.searches[0][1] == "1:*"
    assert result.cursor == InboxCursor(42, 11)
    assert [item.uid for item in result.items] == [10, 11]
    assert result.items[0].identity == f"gmail:{OWN}:message:1010"


def test_malformed_mail_is_returned_as_rejection_without_losing_later_mail(imap):
    imap.data[10][b"BODY[]"] = b"From: invalid\r\n\r\nM12205"
    result = GmailClient(OWN, "secret", start_after=START).poll(InboxCursor(42, 9))
    assert result.items[0].email is None
    assert result.items[0].parse_error
    assert result.items[1].email is not None
    assert result.cursor.last_uid == 11


@pytest.mark.parametrize("missing_field", [b"BODY[]", b"INTERNALDATE", b"RFC822.SIZE"])
def test_incomplete_fetch_raises_without_returning_an_advanced_cursor(imap, missing_field):
    del imap.data[10][missing_field]
    with pytest.raises(OSError, match="fetch"):
        GmailClient(OWN, "secret", start_after=START).poll(InboxCursor(42, 9))


def test_missing_fetch_record_does_not_discard_a_request(imap):
    imap.fetch = lambda _uids, _fields: {}
    with pytest.raises(OSError, match="fetch"):
        GmailClient(OWN, "secret", start_after=START).poll(InboxCursor(42, 9))


def test_missing_routing_headers_are_a_read_failure(imap):
    del imap.data[10][b"BODY[HEADER.FIELDS (TO)]"]
    with pytest.raises(OSError, match="routing-header fetch"):
        GmailClient(
            OWN, "secret", start_after=START, recipient_filter="docket+agent@example.com"
        ).poll(InboxCursor(42, 9))


def test_find_sent_uses_special_folder_and_exact_id(imap):
    imap.uids = [1]
    imap.data = {
        1: {
            b"BODY[HEADER.FIELDS (MESSAGE-ID FROM)]": b"Message-ID: <reply@example.com>\r\nFrom: docket@example.com\r\n\r\n"
        }
    }
    client = GmailClient(OWN, "secret", start_after=START)
    assert client.find_sent("<reply@example.com>") is True
    assert imap.selected[-1] == ("Localized Sent Folder", True)
    assert client.find_sent("<different@example.com>") is False


class FakeSMTP:
    data_error = None
    quit_error = None
    mail_error = None

    def login(self, *_args):
        pass

    def mail(self, _sender):
        if self.mail_error:
            raise self.mail_error
        return 250, b"OK"

    def rcpt(self, _recipient):
        return 250, b"OK"

    def data(self, _raw):
        if self.data_error:
            raise self.data_error
        return 250, b"accepted"

    def quit(self):
        if self.quit_error:
            raise self.quit_error

    def close(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    instance = FakeSMTP()
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", lambda *args, **kwargs: instance)
    return instance


def test_disconnect_during_data_is_uncertain(smtp):
    smtp.data_error = smtplib.SMTPServerDisconnected("lost after upload")
    with pytest.raises(UncertainSend):
        GmailClient(OWN, "secret", start_after=START).send(outgoing())


def test_explicit_data_rejection_is_not_uncertain(smtp):
    smtp.data_error = smtplib.SMTPDataError(552, b"too large")
    with pytest.raises(smtplib.SMTPDataError):
        GmailClient(OWN, "secret", start_after=START).send(outgoing())


def test_disconnect_before_data_is_not_uncertain(smtp):
    smtp.mail_error = smtplib.SMTPServerDisconnected("lost before upload")
    with pytest.raises(smtplib.SMTPServerDisconnected):
        GmailClient(OWN, "secret", start_after=START).send(outgoing())


def test_quit_failure_after_acceptance_is_success(smtp):
    smtp.quit_error = smtplib.SMTPServerDisconnected("lost after acceptance")
    GmailClient(OWN, "secret", start_after=START).send(outgoing())


def test_send_validates_headers_without_parsing_attachment_body(smtp, monkeypatch):
    raw = outgoing() + b"x" * 1_000_000
    original = mail.BytesParser
    parsed = []

    class HeaderOnlyParser:
        def __init__(self, **kwargs):
            self.parser = original(**kwargs)

        def parsebytes(self, value):
            parsed.append(value)
            return self.parser.parsebytes(value)

    sent = []
    monkeypatch.setattr(mail, "BytesParser", HeaderOnlyParser)
    monkeypatch.setattr(smtp, "data", lambda value: (sent.append(value) or 250, b"accepted"))
    GmailClient(OWN, "secret", start_after=START).send(raw)
    assert len(parsed) == 1 and len(parsed[0]) < 2_000
    assert sent == [raw]


def test_send_rejects_additional_recipients(smtp):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = OWN
    message["To"] = "a@example.com, b@example.com"
    message["Message-ID"] = "<reply@example.com>"
    message.set_content("x")
    with pytest.raises(InvalidMessage):
        GmailClient(OWN, "secret", start_after=START).send(message.as_bytes())


def test_alias_filter_checks_routing_headers_before_fetching_any_body(imap):
    alias = "docket+agent@example.com"
    imap.data[9][b"BODY[HEADER.FIELDS (TO)]"] = (
        b'To: "docket+agent@example.com" <personal@example.com>\r\n\r\n'
    )
    imap.data[10][b"BODY[HEADER.FIELDS (TO)]"] = b"To: docket+agent@example.com.invalid\r\n\r\n"
    imap.data[11][b"BODY[HEADER.FIELDS (TO)]"] = b"To: Docket <docket+agent@example.com>\r\n\r\n"
    requested = []
    original_fetch = imap.fetch

    def tracked_fetch(uids, fields):
        requested.append((list(uids), fields))
        return original_fetch(uids, fields)

    imap.fetch = tracked_fetch
    result = GmailClient(OWN, "secret", start_after=START, recipient_filter=alias).poll(None)
    assert imap.searches[0][-2:] == ["TO", alias]
    assert requested[0][1] == [
        "RFC822.SIZE",
        "INTERNALDATE",
        "X-GM-MSGID",
        "BODY.PEEK[HEADER.FIELDS (TO)]",
    ]
    assert requested[1] == ([11], ["BODY.PEEK[]"])
    assert [item.uid for item in result.items] == [11]
    assert result.cursor.last_uid == 11


def test_alias_filter_does_not_fetch_body_when_no_exact_recipient(imap):
    result = GmailClient(
        OWN, "secret", start_after=START, recipient_filter="docket+agent@example.com"
    ).poll(None)
    assert result.items == []
    assert len(imap.fetches) == 1
    assert "BODY.PEEK[]" not in imap.fetches[0]


@pytest.mark.parametrize("header", ["Cc", "Delivered-To"])
def test_alias_in_other_routing_header_cannot_authorize_body_fetch(imap, header):
    alias = "docket+agent@example.com"
    for fields in imap.data.values():
        fields[b"BODY[HEADER.FIELDS (TO)]"] = (
            f"To: {alias}.invalid\r\n{header}: {alias}\r\n\r\n".encode()
        )
    result = GmailClient(OWN, "secret", start_after=START, recipient_filter=alias).poll(None)
    assert result.items == []
    assert result.cursor == InboxCursor(42, 11)
    assert imap.fetches == [
        ["RFC822.SIZE", "INTERNALDATE", "X-GM-MSGID", "BODY.PEEK[HEADER.FIELDS (TO)]"]
    ]


def test_reply_to_alias_keeps_smtp_sender_primary():
    reply = build_reply(
        normalize_inbound(raw_message().as_bytes()),
        Reply(subject="Ready", text="Done"),
        [],
        OWN,
        message_id="<reply@example.com>",
        reply_to="docket+agent@example.com",
    )
    assert reply["From"] == OWN
    assert reply["Reply-To"] == "docket+agent@example.com"


def test_parent_header_alone_preserves_thread_context():
    message = raw_message()
    message["In-Reply-To"] = "<parent@example.com>"
    assert normalize_inbound(message.as_bytes()).references == ["<parent@example.com>"]


def test_oversized_and_old_messages_are_not_downloaded(imap):
    imap.data[9][b"RFC822.SIZE"] = 50_000_000
    imap.data[10][b"INTERNALDATE"] = START - timedelta(seconds=1)
    requested = []
    original = imap.fetch

    def track(uids, fields):
        requested.append((list(uids), fields))
        return original(uids, fields)

    imap.fetch = track
    result = GmailClient(OWN, "secret", start_after=START).poll(None)
    bodies = [uids for uids, fields in requested if fields == ["BODY.PEEK[]"]]
    assert bodies == [[11]]
    assert result.items[0].parse_error == "Incoming message exceeds 10 MB"
    assert result.items[1].email is not None
    assert result.cursor.last_uid == 11
