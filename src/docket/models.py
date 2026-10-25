from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PORTAL_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocType(StrEnum):
    EXHIBITS = "Exhibits"
    KEY_DOCUMENTS = "Key Documents"
    OTHER_DOCUMENTS = "Other Documents"
    TRANSCRIPTS = "Transcripts"
    RECORDINGS = "Recordings"


class FetchRequest(Model):
    matter: str = Field(pattern=r"^M\d{5}$")
    doc_type: DocType
    limit: int = Field(default=10, ge=1, le=10)


class Decision(Model):
    action: Literal["deliver", "clarify", "report", "no_reply"]
    reason: Literal[
        "completed",
        "missing_matter",
        "missing_category",
        "ambiguous_request",
        "unsupported_category",
        "empty_category",
        "matter_not_found",
        "service_unavailable",
        "no_accessible_files",
        "acknowledgement",
        "invalid_request",
    ]
    matter: str | None = Field(default=None, pattern=r"^M\d{5}$")
    category: DocType | None = None
    limit: int | None = Field(default=None, ge=1, le=10)


class MatterMeta(Model):
    matter: str
    title: str
    status: str = ""
    type: str = ""
    category: str = ""
    date_received: str = ""
    second_date: str = ""
    second_date_label: str = ""
    outcome: str = ""
    counts: dict[DocType, int]

    @model_validator(mode="after")
    def complete_counts(self):
        if set(self.counts) != set(DocType) or any(value < 0 for value in self.counts.values()):
            raise ValueError("All five category counters must be present and nonnegative")
        return self


class DocRow(Model):
    doc_type: DocType
    doc_id: str
    title: str
    filed: str = ""
    security: str = ""
    extension: str = ""
    position: int
    has_portal_id: bool = True

    @property
    def downloadable(self) -> bool:
        label = self.security.strip().casefold()
        return label == "public" or (
            not label and self.doc_type in {DocType.TRANSCRIPTS, DocType.RECORDINGS}
        )


class FetchedFile(Model):
    row: DocRow
    path: Path
    served_name: str
    size: int = Field(ge=1)
    sha256: str
    content_type: str
    retrieved_at: datetime = Field(default_factory=utc_now)


class Skipped(Model):
    row: DocRow
    reason: str
    detail: str = ""


class TabResult(Model):
    doc_type: DocType
    count: int
    rows_read: list[DocRow] = Field(default_factory=list)
    all_rows_read: bool = False
    files: list[FetchedFile] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)


class FetchResult(Model):
    request: FetchRequest
    meta: MatterMeta
    tab: TabResult
    retrieved_at: datetime = Field(default_factory=utc_now)
    timings: dict[str, float] = Field(default_factory=dict)
    selection: str = (
        "First accessible documents in the portal's displayed order, up to the requested limit."
    )


class Bundle(Model):
    path: Path
    file_count: int
    size: int
    sha256: str


class Reply(Model):
    subject: str
    text: str


class InboundEmail(Model):
    message_id: str
    sender: str
    subject: str = ""
    text: str = ""
    references: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utc_now)
