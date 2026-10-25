"""Build independently openable ZIPs containing original bytes and provenance."""

import csv
import hashlib
import io
import json
import os
import re
import shutil
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .files import digest
from .models import PORTAL_URL, Bundle, FetchedFile, FetchResult, Skipped


def _slug(value: str, length: int = 70) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:length] or "document"


def _csv_cell(value) -> str:
    value = str(value).replace("\x00", "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _entry(file: FetchedFile, index: int) -> dict:
    suffix = Path(file.served_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    return {
        "filename": f"{index:02d}_{_slug(file.row.doc_id, 40)}_{_slug(file.row.title)}{suffix}",
        "document_id": file.row.doc_id,
        "has_portal_id": file.row.has_portal_id,
        "title": file.row.title,
        "date": file.row.filed,
        "category": file.row.doc_type.value,
        "security": file.row.security,
        "portal_position": file.row.position,
        "served_filename": file.served_name,
        "size_bytes": file.size,
        "sha256": file.sha256,
        "retrieved_at": file.retrieved_at.isoformat(),
        "outcome": "attached",
    }


def _manifests(
    result: FetchResult, entries: list[dict], skipped: list[Skipped]
) -> dict[str, bytes]:
    manifest = {
        "source_url": PORTAL_URL,
        "matter": result.meta.model_dump(mode="json"),
        "request": result.request.model_dump(mode="json"),
        "retrieved_at": result.retrieved_at.isoformat(),
        "selection": result.selection,
        "rows_examined": len(result.tab.rows_read),
        "all_rows_examined": result.tab.all_rows_read,
        "files": entries,
        "omissions": [item.model_dump(mode="json") for item in skipped],
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(entries[0].keys())
    writer.writerows([_csv_cell(value) for value in entry.values()] for entry in entries)
    return {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
        "manifest.csv": output.getvalue().encode("utf-8-sig"),
    }


def _archive_size(folder: str, entries: list[dict], manifests: dict[str, bytes]) -> int:
    # ZIP_STORED, no extra fields/comments/ZIP64: local + central headers + EOCD.
    members = [(entry["filename"], entry["size_bytes"]) for entry in entries]
    members.extend((name, len(data)) for name, data in manifests.items())
    return 22 + sum(size + 76 + 2 * len(f"{folder}/{name}".encode()) for name, size in members)


def build_bundles(
    result: FetchResult,
    out_dir: Path,
    budget_bytes: int = 18_000_000,
) -> tuple[list[Bundle], list[Skipped]]:
    if (
        result.meta.matter != result.request.matter
        or result.tab.doc_type != result.request.doc_type
    ):
        raise ValueError("The source result does not match the request")
    out_dir.mkdir(parents=True, exist_ok=True)
    accepted: list[tuple[FetchedFile, dict]] = []
    skipped: list[Skipped] = []
    for file in result.tab.files:
        reason = detail = ""
        if file.row.doc_type != result.request.doc_type or not file.row.downloadable:
            reason, detail = "not_public", "The source row is not public in the requested category."
        else:
            try:
                if file.path.stat().st_size != file.size or digest(file.path) != file.sha256:
                    raise ValueError("File size or SHA-256 changed after retrieval.")
            except (OSError, ValueError) as exc:
                reason, detail = "integrity", str(exc)
        if not reason and len(accepted) >= result.request.limit:
            reason, detail = "file_limit", "The requested file limit was reached."
        if reason:
            skipped.append(Skipped(row=file.row, reason=reason, detail=detail))
        else:
            accepted.append((file, _entry(file, len(accepted) + 1)))

    prefix = f"{result.request.matter}_{_slug(result.request.doc_type.value).lower()}"
    # Omissions themselves occupy space. Settle them before measuring any group;
    # each iteration removes files, so this terminates within the request limit.
    while accepted:
        manifests_skipped = result.tab.skipped + skipped
        fitting = []
        for pair in accepted:
            entries = [pair[1]]
            manifests = _manifests(result, entries, manifests_skipped)
            if _archive_size(f"{prefix}_part01", entries, manifests) > budget_bytes:
                skipped.append(
                    Skipped(
                        row=pair[0].row,
                        reason="too_large",
                        detail="Original plus manifest exceeds the ZIP email budget.",
                    )
                )
            else:
                fitting.append(pair)
        if len(fitting) == len(accepted):
            break
        accepted = fitting

    # Fixed-width part names keep header overhead the same when regrouping.
    groups: list[list[tuple[FetchedFile, dict]]] = []
    for pair in accepted:
        candidate = (groups[-1] if groups else []) + [pair]
        folder = f"{prefix}_part{max(1, len(groups)):02d}"
        entries = [entry for _, entry in candidate]
        manifests = _manifests(result, entries, result.tab.skipped + skipped)
        if _archive_size(folder, entries, manifests) <= budget_bytes:
            if groups:
                groups[-1] = candidate
            else:
                groups.append(candidate)
            continue
        groups.append([pair])

    bundles = []
    for number, group in enumerate(groups, 1):
        folder = f"{prefix}_part{number:02d}"
        path = out_dir / f"{folder}.zip"
        temporary = path.with_suffix(".zip.tmp")
        entries = [entry for _, entry in group]
        manifests = _manifests(result, entries, result.tab.skipped + skipped)
        timestamp = result.retrieved_at.timetuple()[:6]
        timestamp = (min(2107, max(1980, timestamp[0])), *timestamp[1:])

        def info(name, folder=folder, timestamp=timestamp):
            member = ZipInfo(f"{folder}/{name}", timestamp)
            member.compress_type = ZIP_STORED
            member.external_attr = 0o100644 << 16
            return member

        try:
            with ZipFile(temporary, "w", compression=ZIP_STORED, allowZip64=False) as archive:
                for file, entry in group:
                    with (
                        file.path.open("rb") as source,
                        archive.open(info(entry["filename"]), "w") as target,
                    ):
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                for name, data in manifests.items():
                    archive.writestr(info(name), data)
            if temporary.stat().st_size > budget_bytes:
                raise ValueError(
                    "ZIP including complete omission metadata exceeds the email budget"
                )
            with ZipFile(temporary) as archive:
                for _, entry in group:
                    # Reading each member to EOF verifies ZIP CRC as well as SHA-256.
                    with archive.open(f"{folder}/{entry['filename']}") as source:
                        if hashlib.file_digest(source, "sha256").hexdigest() != entry["sha256"]:
                            raise ValueError("Source changed while packaging")
                for name, data in manifests.items():
                    if archive.read(f"{folder}/{name}") != data:
                        raise ValueError("ZIP manifest verification failed")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(path)
            bundles.append(
                Bundle(
                    path=path, file_count=len(group), size=path.stat().st_size, sha256=digest(path)
                )
            )
        finally:
            temporary.unlink(missing_ok=True)
    return bundles, skipped
