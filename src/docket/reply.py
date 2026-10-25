"""Reply facts come from verified portal fields and the ZIPs being attached."""

import json
from collections import defaultdict
from zipfile import ZipFile

from .models import PORTAL_URL, Bundle, DocType, FetchResult, MatterMeta, Reply, Skipped


def _matter_lines(meta: MatterMeta) -> list[str]:
    lines = [f"{meta.matter} — {meta.title}", ""]
    for label, value in [
        ("Status", meta.status),
        ("Type", meta.type),
        ("Category", meta.category),
        ("Date received", meta.date_received),
        (meta.second_date_label or "Second date", meta.second_date),
        ("Outcome", meta.outcome),
    ]:
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(["", "Listed entries, including non-public entries:"])
    lines.extend(f"  {kind.value}: {meta.counts[kind]}" for kind in DocType)
    total = sum(count for kind, count in meta.counts.items() if kind != DocType.KEY_DOCUMENTS)
    lines.extend(
        [
            f"Total excluding repeated Key Documents: {total} listed entries.",
            (
                "Key Documents are also under Other Documents. An entry can contain several files; "
                "this is not an independently reconciled unique-file count."
            ),
            "Counting guide: https://nserbt.ca/matters-evidence/how-use-database",
            "",
        ]
    )
    return lines


def render_inspection(meta: MatterMeta, category: DocType) -> Reply:
    if meta.counts[category] != 0:
        raise ValueError("An empty-category reply requires a verified zero counter.")
    return Reply(
        subject=f"{meta.matter}: no {category.value}",
        text="\n".join(
            [
                *_matter_lines(meta),
                f"No {category.value} are listed; no ZIP is attached.",
                f"Source: {PORTAL_URL}",
            ]
        ),
    )


def render(result: FetchResult, bundles: list[Bundle], skipped: list[Skipped]) -> Reply:
    files = []
    for bundle in bundles:
        with ZipFile(bundle.path) as archive:
            name = next(name for name in archive.namelist() if name.endswith("/manifest.json"))
            files.extend(json.loads(archive.read(name))["files"])
    category = result.request.doc_type.value
    count = result.tab.count
    lines = _matter_lines(result.meta)
    if count == 0:
        lines.append(f"No {category} are listed; no ZIP is attached.")
    elif files:
        rows = {(file["document_id"], file["portal_position"]) for file in files}
        lines.append(
            f"Attached {len(files)} original file(s) from {len(rows)} of {count} listed "
            f"{category} entries, in {len(bundles)} ZIP(s)."
        )
        lines.append("Attached source IDs: " + ", ".join(file["document_id"] for file in files))
    else:
        lines.append(f"No original {category} files could be attached. See the omissions below.")
    lines.append(f"Selection: {result.selection}")
    lines.append(f"Examined {len(result.tab.rows_read)} of {count} entries.")
    if any(not item["security"].strip() for item in files):
        lines.append(
            "Unlabelled transcript/recording rows were served by the public portal without authentication."
        )
    if not result.tab.all_rows_read:
        lines.append("The full category was not scanned; omissions cover encountered entries only.")

    hashes = defaultdict(list)
    for file in files:
        hashes[file["sha256"]].append(file["document_id"])
    duplicates = [ids for ids in hashes.values() if len(ids) > 1]
    if duplicates:
        lines.extend(["", "Distinct source records with identical downloaded bytes (preserved):"])
        lines.extend("  " + ", ".join(ids) for ids in duplicates)

    omissions = {
        (item.row.doc_id, item.reason, item.detail): item
        for item in [*result.tab.skipped, *skipped]
    }
    if omissions:
        lines.extend(["", "Encountered omissions:"])
        for item in list(omissions.values())[:10]:
            reason = item.reason.replace("_", " ")
            lines.append(f"  {item.row.doc_id}: {reason}. {item.detail}".rstrip())
        if len(omissions) > 10:
            if bundles:
                lines.append(
                    f"{len(omissions) - 10} additional omissions are recorded in the ZIP manifests."
                )
            else:
                lines.append(f"{len(omissions) - 10} additional omissions are not shown here.")
    lines.extend(["", f"Source: {PORTAL_URL}", f"Retrieved: {result.retrieved_at.isoformat()}"])
    if bundles:
        lines.append(
            "ZIP manifests include source metadata, sizes and SHA-256 hashes. Original bytes are unchanged."
        )
    return Reply(
        subject=f"{result.meta.matter} {category}: {len(files)} file(s) attached",
        text="\n".join(lines),
    )
