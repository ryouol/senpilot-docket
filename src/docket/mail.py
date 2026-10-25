"""Gmail transport with durable cursors and explicit SMTP uncertainty.

Persist accepted PollResult.items and log rejected items before its cursor. A
cursor is not an acknowledgement; poll never changes message flags. Persist a
reply's exact bytes and Message-ID before send. On UncertainSend, reconcile with
find_sent instead of immediately retrying; a negative lookup is not proof that
SMTP did not accept the message.
"""

from __future__ import annotations

import hashlib
import re
import smtplib
import ssl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy
from email.errors import MessageError
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser

from imapclient import IMAPClient

from .models import Bundle, InboundEmail, Reply

MAX_MESSAGE_BYTES = 25_000_000
MAX_INBOUND_BYTES = 10_000_000
_MESSAGE_ID = re.compile(r"<[^\s<>@]+@[^\s<>@]+>")


class InvalidMessage(ValueError):
    """The message cannot safely be interpreted or addressed."""


class MessageTooLarge(ValueError):
    """The fully serialized message exceeds the transport budget."""


class UncertainSend(RuntimeError):
    """SMTP DATA may have been accepted; do not blindly retry."""


@dataclass(frozen=True, slots=True)
class InboxCursor:
    uidvalidity: int
    last_uid: int = 0


@dataclass(frozen=True, slots=True)
class MailItem:
    identity: str
    uid: int
    email: InboundEmail | None
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    cursor: InboxCursor
    items: list[MailItem]


def _clean_header(value: str, limit: int = 500) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()[:limit]


def _address(value: str) -> str:
    """Accept exactly one ordinary mailbox, never groups or header injection."""
    if not value or any(c in value for c in "\r\n\x00"):
        raise InvalidMessage("Expected one email address without control characters")
    probe = EmailMessage(policy=policy.default)
    try:
        probe["To"] = value
        header = probe["To"]
        addresses = header.addresses
        if header.defects or len(addresses) != 1:
            raise ValueError("ambiguous address")
        if any(group.display_name is not None for group in header.groups):
            raise ValueError("address groups are unsupported")
        address = addresses[0]
        if not address.username or not address.domain:
            raise ValueError("incomplete address")
        # This transport deliberately does not negotiate SMTPUTF8.
        address.addr_spec.encode("ascii")
        return address.addr_spec
    except (ValueError, AttributeError, IndexError, UnicodeError, MessageError) as exc:
        raise InvalidMessage("Expected exactly one valid ASCII email address") from exc


def _single_header(message: Message, name: str, *, required: bool = False) -> str:
    values = message.get_all(name, [])
    if len(values) > 1 or (required and not values):
        raise InvalidMessage(f"Expected exactly one {name} header")
    return str(values[0]) if values else ""


def _validate_message_id(value: str) -> str:
    if len(value) > 255 or not _MESSAGE_ID.fullmatch(value):
        raise InvalidMessage("Expected a single bracketed Message-ID")
    return value


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "tr", "td", "th"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr", "td", "th"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _body_parts(part: Message) -> Iterator[tuple[str, str]]:
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return
    if part.is_multipart():
        for child in part.get_payload():
            yield from _body_parts(child)
    elif part.get_content_type() in {"text/plain", "text/html"}:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        yield part.get_content_type(), text


def normalize_inbound(raw: bytes) -> InboundEmail:
    """Decode MIME without reading attachments or trusting Reply-To recipients."""
    if len(raw) > MAX_INBOUND_BYTES:
        raise InvalidMessage("Incoming message exceeds the 10 MB parsing budget")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = _address(_single_header(message, "From", required=True))
    header_id = _single_header(message, "Message-ID")
    message_id = (
        header_id
        if _MESSAGE_ID.fullmatch(header_id) and len(header_id) <= 255
        else f"<{hashlib.sha256(raw).hexdigest()}@missing-message-id.docket.invalid>"
    )
    parts = list(_body_parts(message))
    plain = [text for content_type, text in parts if content_type == "text/plain"]
    if plain:
        text = "\n".join(plain)
    else:
        parser = _HTMLText()
        parser.feed("\n".join(value for _, value in parts))
        text = "".join(parser.parts)
    references = _MESSAGE_ID.findall(_single_header(message, "References"))[-20:]
    references.extend(_MESSAGE_ID.findall(_single_header(message, "In-Reply-To")))
    try:
        received_at = parsedate_to_datetime(_single_header(message, "Date"))
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)
    except (ValueError, TypeError, OverflowError):
        received_at = datetime.fromtimestamp(0, UTC)
    headers: dict[str, str] = {}
    for name, value in message.items():
        key = name.lower()
        clean = _clean_header(str(value), limit=2_000)
        headers[key] = f"{headers[key]}, {clean}" if key in headers else clean
    return InboundEmail(
        message_id=message_id,
        sender=sender,
        subject=_clean_header(_single_header(message, "Subject")),
        # A request above the parser's limit stays above it, without retaining
        # multi-megabyte MIME bodies in the durable job queue.
        text=text.replace("\x00", "").strip()[:16_001],
        references=list(dict.fromkeys(ref for ref in references if len(ref) <= 255))[-20:],
        headers=headers,
        received_at=received_at,
    )


def ignore_reason(email: InboundEmail, own_address: str) -> str | None:
    """Return a stable reason for messages that must not cause automatic replies."""
    headers = {key.lower(): value.strip().lower() for key, value in email.headers.items()}
    if _address(email.sender).casefold() == _address(own_address).casefold():
        return "own_message"
    auto = headers.get("auto-submitted", "")
    if auto and auto != "no":
        return "automatic_message"
    if headers.get("return-path") == "<>":
        return "delivery_report"
    content_type = headers.get("content-type", "")
    if "multipart/report" in content_type or "message/delivery-status" in content_type:
        return "delivery_report"
    if headers.get("precedence") in {"bulk", "list", "junk"} or headers.get("list-id"):
        return "mailing_list"
    suppression = re.split(r"[,;\s]+", headers.get("x-auto-response-suppress", ""))
    if "all" in suppression or "autoreply" in suppression:
        return "auto_reply_suppressed"
    local_part = email.sender.rsplit("@", 1)[0].casefold()
    if local_part in {"mailer-daemon", "postmaster", "noreply", "no-reply"}:
        return "automated_sender"
    return None


def serialize_message(message: EmailMessage) -> bytes:
    raw = message.as_bytes(policy=policy.SMTP)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge("Serialized email exceeds 25,000,000 bytes")
    return raw


def build_reply(
    inbound: InboundEmail,
    reply: Reply,
    bundles: list[Bundle],
    own_address: str,
    *,
    message_id: str,
    reply_to: str | None = None,
) -> EmailMessage:
    """Build one threaded reply; caller persists message_id and serialized bytes."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = _address(own_address)
    message["To"] = _address(inbound.sender)
    if reply_to:
        message["Reply-To"] = _address(reply_to)
    message["Subject"] = _clean_header(reply.subject or f"Re: {inbound.subject}")
    message["Message-ID"] = _validate_message_id(message_id)
    message["Date"] = format_datetime(datetime.now(UTC))
    parent_id = _validate_message_id(inbound.message_id)
    message["In-Reply-To"] = parent_id
    references = list(dict.fromkeys([*inbound.references, parent_id]))[-20:]
    message["References"] = " ".join(_validate_message_id(value) for value in references)
    message["Auto-Submitted"] = "auto-replied"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(reply.text)
    # Check the declared and actual size before allocating encoded attachments.
    if sum(bundle.path.stat().st_size for bundle in bundles) > MAX_MESSAGE_BYTES:
        raise MessageTooLarge("Attachments exceed the email transport budget")
    for bundle in bundles:
        payload = bundle.path.read_bytes()
        if len(payload) != bundle.size or hashlib.sha256(payload).hexdigest() != bundle.sha256:
            raise InvalidMessage("Bundle changed after packaging")
        filename = _clean_header(bundle.path.name, limit=200)
        message.add_attachment(payload, maintype="application", subtype="zip", filename=filename)
    return message


class GmailClient:
    """Synchronous, bounded I/O; caller owns scheduling, retries, and persistence."""

    def __init__(
        self,
        address: str,
        app_password: str,
        *,
        start_after: datetime,
        timeout: float = 30,
        max_messages: int = 25,
        imap_host: str = "imap.gmail.com",
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 465,
        recipient_filter: str | None = None,
    ) -> None:
        if start_after.tzinfo is None:
            raise ValueError("start_after must have a timezone")
        if max_messages < 1 or timeout <= 0:
            raise ValueError("max_messages and timeout must be positive")
        self.address = _address(address)
        self._password = app_password.replace(" ", "")
        self.start_after = start_after.astimezone(UTC)
        self.timeout = timeout
        self.max_messages = max_messages
        self.imap_host = imap_host
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.recipient_filter = _address(recipient_filter) if recipient_filter else None

    @contextmanager
    def _imap(self) -> Iterator[IMAPClient]:
        connection = IMAPClient(self.imap_host, ssl=True, timeout=self.timeout)
        connection.normalise_times = False
        try:
            connection.login(self.address, self._password)
            yield connection
        finally:
            with suppress(OSError, IMAPClient.Error):
                connection.logout()

    def poll(self, cursor: InboxCursor | None) -> PollResult:
        """Reconcile bounded UID batches, irrespective of read/unread state.

        Gmail's X-GM-MSGID makes each identity stable even if UIDVALIDITY changes.
        A UID-based fallback is included for test servers without this extension.
        """
        with self._imap() as connection:
            selected = connection.select_folder("INBOX", readonly=True)
            validity = int(selected[b"UIDVALIDITY"])
            last_uid = cursor.last_uid if cursor and cursor.uidvalidity == validity else 0
            # IMAP SINCE has day precision; the extra day covers timezone boundaries.
            since = (self.start_after - timedelta(days=1)).date()
            criteria = ["UID", f"{last_uid + 1}:*", "SINCE", since]
            if self.recipient_filter:
                criteria.extend(["TO", self.recipient_filter])
            candidates = connection.search(criteria)
            uids = sorted({int(uid) for uid in candidates if int(uid) > last_uid})
            uids = uids[: self.max_messages]
            if not uids:
                return PollResult(InboxCursor(validity, last_uid), [])
            fields_requested = ["RFC822.SIZE", "INTERNALDATE", "X-GM-MSGID"]
            if self.recipient_filter:
                fields_requested.append("BODY.PEEK[HEADER.FIELDS (TO)]")
            fetched = connection.fetch(uids, fields_requested)
            items: list[MailItem] = []
            for uid in uids:
                fields = fetched.get(uid, {})
                if self.recipient_filter:
                    # TO searches substrings; verify the exact route before reading a body.
                    if not any(
                        key.startswith(b"BODY[HEADER.FIELDS") and isinstance(value, bytes)
                        for key, value in fields.items()
                    ):
                        raise IMAPClient.Error("Inbox routing-header fetch was incomplete")
                    if not self._matches_recipient(fields):
                        continue
                gmail_id = fields.get(b"X-GM-MSGID")
                suffix = f"message:{gmail_id}" if gmail_id else f"INBOX:{validity}:{uid}"
                identity = f"gmail:{self.address.casefold()}:{suffix}"
                received = fields.get(b"INTERNALDATE")
                if isinstance(received, datetime) and received.tzinfo is not None:
                    if received < self.start_after:
                        continue
                else:
                    raise IMAPClient.Error("Inbox fetch omitted an aware INTERNALDATE")
                size = fields.get(b"RFC822.SIZE")
                if not isinstance(size, int) or size < 0:
                    raise IMAPClient.Error("Inbox fetch omitted the message size")
                if size > MAX_INBOUND_BYTES:
                    items.append(MailItem(identity, uid, None, "Incoming message exceeds 10 MB"))
                    continue
                raw = connection.fetch([uid], ["BODY.PEEK[]"]).get(uid, {}).get(b"BODY[]")
                if not isinstance(raw, bytes):
                    # A partial FETCH is a transient read failure, not malformed
                    # email. Returning an advanced cursor here would lose a job.
                    raise IMAPClient.Error("Inbox body fetch was incomplete")
                try:
                    email = normalize_inbound(raw).model_copy(update={"received_at": received})
                    items.append(MailItem(identity, uid, email))
                except (InvalidMessage, ValueError) as exc:
                    items.append(MailItem(identity, uid, None, str(exc)))
            return PollResult(InboxCursor(validity, uids[-1]), items)

    def _matches_recipient(self, fields: dict) -> bool:
        for key, raw in fields.items():
            if not key.startswith(b"BODY[HEADER.FIELDS") or not isinstance(raw, bytes):
                continue
            message = BytesParser(policy=policy.default).parsebytes(raw)
            for header in message.get_all("To", []):
                try:
                    if header.defects:
                        continue
                    if any(
                        address.addr_spec.casefold() == self.recipient_filter.casefold()
                        for address in header.addresses
                    ):
                        return True
                except (MessageError, AttributeError):
                    continue
        return False

    def send(self, raw: bytes) -> None:
        """Send once; explicit DATA rejection is safe to distinguish from uncertainty."""
        if len(raw) > MAX_MESSAGE_BYTES:
            raise MessageTooLarge("Serialized email exceeds 25,000,000 bytes")
        header_end = raw.find(b"\r\n\r\n")
        if not 0 < header_end < 65_536:
            raise InvalidMessage("Prepared email headers are missing or too large")
        message = BytesParser(policy=policy.default).parsebytes(raw[: header_end + 4])
        sender = _address(_single_header(message, "From", required=True))
        recipient = _address(_single_header(message, "To", required=True))
        _validate_message_id(_single_header(message, "Message-ID", required=True))
        if sender.casefold() != self.address.casefold() or message.get("Cc") or message.get("Bcc"):
            raise InvalidMessage("Only one direct recipient and the configured sender are allowed")
        connection = smtplib.SMTP_SSL(
            self.smtp_host,
            self.smtp_port,
            timeout=self.timeout,
            context=ssl.create_default_context(),
        )
        try:
            connection.login(self.address, self._password)
            code, response = connection.mail(self.address)
            if code != 250:
                raise smtplib.SMTPSenderRefused(code, response, self.address)
            code, response = connection.rcpt(recipient)
            if code not in {250, 251}:
                raise smtplib.SMTPRecipientsRefused({recipient: (code, response)})
            try:
                code, response = connection.data(raw)
            except smtplib.SMTPResponseException:
                # SMTPException inherits OSError: preserve explicit server rejection.
                raise
            except (smtplib.SMTPServerDisconnected, OSError) as exc:
                raise UncertainSend(
                    "Connection lost during SMTP DATA; reconcile Sent Mail"
                ) from exc
            if code != 250:
                raise smtplib.SMTPDataError(code, response)
        finally:
            # A lost QUIT response after accepted DATA must not trigger a duplicate send.
            try:
                connection.quit()
            except (smtplib.SMTPException, OSError):
                connection.close()

    def find_sent(self, message_id: str) -> bool:
        """An exact match confirms acceptance; no match does not establish non-delivery."""
        _validate_message_id(message_id)
        with self._imap() as connection:
            folders = connection.list_folders()
            sent_folder = next(
                (
                    name
                    for flags, _, name in folders
                    if any(
                        (flag.decode() if isinstance(flag, bytes) else flag).casefold() == "\\sent"
                        for flag in flags
                    )
                ),
                "[Gmail]/Sent Mail",
            )
            connection.select_folder(sent_folder, readonly=True)
            uids = connection.search(["HEADER", "Message-ID", message_id])
            if not uids:
                return False
            data = connection.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM)]"])
            for fields in data.values():
                for key, value in fields.items():
                    if not key.startswith(b"BODY[") or not isinstance(value, bytes):
                        continue
                    message = BytesParser(policy=policy.default).parsebytes(value)
                    if _single_header(message, "Message-ID") == message_id:
                        sender = _address(_single_header(message, "From", required=True))
                        if sender.casefold() == self.address.casefold():
                            return True
            return False
