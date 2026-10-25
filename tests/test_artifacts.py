import csv
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from zipfile import ZipFile

import pytest

from docket.mail import build_reply, serialize_message
from docket.models import (
    DocRow,
    DocType,
    FetchedFile,
    FetchRequest,
    FetchResult,
    InboundEmail,
    MatterMeta,
    TabResult,
)
from docket.packager import build_bundles
from docket.reply import render, render_inspection


def sample(tmp_path, payloads, *, limit=10, title="Order"):
    files = []
    for index, payload in enumerate(payloads):
        path = tmp_path / f"source-{index}.pdf"
        path.write_bytes(payload)
        files.append(
            FetchedFile(
                row=DocRow(
                    doc_type=DocType.OTHER_DOCUMENTS,
                    doc_id=str(100 + index),
                    title=title,
                    security="Public",
                    position=index,
                ),
                path=path,
                served_name=f"{100 + index}.pdf",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                content_type="application/pdf",
                retrieved_at=datetime(2026, 10, 24, tzinfo=UTC),
            )
        )
    return FetchResult(
        request=FetchRequest(matter="M12205", doc_type=DocType.OTHER_DOCUMENTS, limit=limit),
        meta=MatterMeta(
            matter="M12205",
            title="Source title",
            status="Closed",
            counts={kind: (43 if kind == DocType.OTHER_DOCUMENTS else 0) for kind in DocType},
        ),
        tab=TabResult(
            doc_type=DocType.OTHER_DOCUMENTS,
            count=43,
            files=files,
            rows_read=[file.row for file in files],
        ),
        retrieved_at=datetime(2026, 10, 24, tzinfo=UTC),
    )


def manifest(archive):
    name = next(name for name in archive.namelist() if name.endswith("/manifest.json"))
    return name.rsplit("/", 1)[0], json.loads(archive.read(name))


def test_original_bytes_manifest_safety_and_deterministic_archives(tmp_path):
    result = sample(tmp_path, [b"%PDF-one", b"%PDF-two"], title="=formula / ../../CON ü")
    bundles, skipped = build_bundles(result, tmp_path / "a")
    second, _ = build_bundles(result, tmp_path / "b")
    assert not skipped
    assert bundles[0].path.read_bytes() == second[0].path.read_bytes()
    with ZipFile(bundles[0].path) as archive:
        folder, data = manifest(archive)
        assert data["matter"]["matter"] == "M12205"
        assert data["source_url"].startswith("https://uarb.novascotia.ca/")
        for entry, original in zip(data["files"], result.tab.files, strict=True):
            assert archive.read(f"{folder}/{entry['filename']}") == original.path.read_bytes()
            assert entry["sha256"] == original.sha256
        assert all(".." not in PurePosixPath(name).parts for name in archive.namelist())
        csv_rows = list(
            csv.DictReader(io.StringIO(archive.read(f"{folder}/manifest.csv").decode("utf-8-sig")))
        )
        assert csv_rows[0]["title"].startswith("'=")
        assert data["files"][0]["title"].startswith("=")


def test_split_archives_are_independent_and_respect_final_budget(tmp_path):
    result = sample(tmp_path, [bytes([index]) * 2000 for index in range(4)])
    bundles, skipped = build_bundles(result, tmp_path / "out", budget_bytes=6500)
    assert len(bundles) > 1 and not skipped
    assert sum(bundle.file_count for bundle in bundles) == 4
    identifiers = []
    for bundle in bundles:
        assert bundle.size <= 6500
        with ZipFile(bundle.path) as archive:
            _, data = manifest(archive)
            identifiers.extend(file["document_id"] for file in data["files"])
            assert archive.testzip() is None
    assert identifiers == ["100", "101", "102", "103"]


@pytest.mark.parametrize("budget", [3100, 6000])
def test_late_omissions_are_accounted_for_before_grouping(tmp_path, budget):
    result = sample(tmp_path, [b"x" * 1000, b"y" * 10000])
    bundles, skipped = build_bundles(result, tmp_path / "out", budget_bytes=budget)
    assert all(bundle.size <= budget for bundle in bundles)
    assert any(item.row.doc_id == "101" and item.reason == "too_large" for item in skipped)
    if budget == 6000:
        assert sum(bundle.file_count for bundle in bundles) == 1


def test_changed_or_nonpublic_sources_are_not_attached(tmp_path):
    result = sample(tmp_path, [b"good", b"tamper", b"private"])
    result.tab.files[1].path.write_bytes(b"change")
    result.tab.files[2].row.security = "Confidential"
    bundles, skipped = build_bundles(result, tmp_path / "out")
    assert sum(bundle.file_count for bundle in bundles) == 1
    assert {item.reason for item in skipped} == {"integrity", "not_public"}


@pytest.mark.parametrize(
    "category,security,allowed",
    [
        (DocType.RECORDINGS, "", True),
        (DocType.RECORDINGS, "Confidential", False),
        (DocType.OTHER_DOCUMENTS, "", False),
    ],
)
def test_unlabelled_recording_policy_preserves_label_and_discloses_access(
    tmp_path, category, security, allowed
):
    result = sample(tmp_path, [b"RIFF\x04\x00\x00\x00WAVE"])
    result.request.doc_type = result.tab.doc_type = category
    result.meta.counts = {kind: int(kind == category) for kind in DocType}
    result.tab.count = 1
    result.tab.all_rows_read = True
    file = result.tab.files[0]
    file.row.doc_type, file.row.security = category, security
    file.served_name = "recording.wav"
    file.content_type = "audio/wav"
    bundles, skipped = build_bundles(result, tmp_path / "out")
    if not allowed:
        assert bundles == []
        assert [item.reason for item in skipped] == ["not_public"]
        return
    assert not skipped
    assert sum(bundle.file_count for bundle in bundles) == 1
    with ZipFile(bundles[0].path) as archive:
        folder, data = manifest(archive)
        entry = data["files"][0]
        assert entry["security"] == ""
        assert archive.read(f"{folder}/{entry['filename']}") == file.path.read_bytes()
        csv_rows = list(
            csv.DictReader(io.StringIO(archive.read(f"{folder}/manifest.csv").decode("utf-8-sig")))
        )
        assert csv_rows[0]["security"] == ""
    reply = render(result, bundles, skipped)
    assert "Unlabelled transcript/recording rows" in reply.text
    assert "without authentication" in reply.text


def test_file_limit_and_request_identity_are_enforced(tmp_path):
    result = sample(tmp_path, [b"a", b"b", b"c"], limit=2)
    bundles, skipped = build_bundles(result, tmp_path / "out")
    assert sum(bundle.file_count for bundle in bundles) == 2
    assert skipped[0].reason == "file_limit"
    result.meta.matter = "M99999"
    with pytest.raises(ValueError, match="match"):
        build_bundles(result, tmp_path / "bad")


def test_reply_preserves_duplicate_records_and_qualifies_partial_counts(tmp_path):
    result = sample(tmp_path, [b"same", b"same"])
    bundles, skipped = build_bundles(result, tmp_path / "out")
    reply = render(result, bundles, skipped)
    assert "100, 101" in reply.text
    assert "identical downloaded bytes" in reply.text
    assert "not an independently reconciled" in reply.text
    assert "full category was not scanned" in reply.text
    assert "Attached 2 original file(s) from 2" in reply.text
    inbound = InboundEmail(message_id="<test@example.com>", sender="requester@example.com")
    message = build_reply(
        inbound, reply, bundles, "agent@example.com", message_id="<reply@example.com>"
    )
    raw = serialize_message(message)
    assert len(raw) < 25_000_000
    assert next(message.iter_attachments()).get_payload(decode=True) == bundles[0].path.read_bytes()


def test_empty_category_has_no_archive_and_clear_reply(tmp_path):
    result = sample(tmp_path, [])
    result.tab.count = 0
    result.tab.all_rows_read = True
    result.meta.counts[DocType.OTHER_DOCUMENTS] = 0
    bundles, skipped = build_bundles(result, tmp_path / "out")
    assert bundles == [] and skipped == []
    assert "No Other Documents are listed" in render(result, bundles, skipped).text


def test_inspection_reply_requires_zero_and_reports_all_counters(tmp_path):
    meta = sample(tmp_path, []).meta
    meta.counts[DocType.EXHIBITS] = 13
    meta.counts[DocType.KEY_DOCUMENTS] = 6
    reply = render_inspection(meta, DocType.TRANSCRIPTS)
    assert "Total excluding repeated Key Documents: 56 listed entries" in reply.text
    assert "No Transcripts are listed; no ZIP is attached" in reply.text
    assert "not an independently reconciled unique-file count" in reply.text
    for kind in DocType:
        assert f"{kind.value}: {meta.counts[kind]}" in reply.text
    with pytest.raises(ValueError, match="verified zero"):
        render_inspection(meta, DocType.OTHER_DOCUMENTS)
