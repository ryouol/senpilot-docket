import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from docket.models import (
    DocRow,
    DocType,
    FetchedFile,
    FetchRequest,
    FetchResult,
    MatterMeta,
    TabResult,
)
from docket.portal import (
    FileRejected,
    MatterNotFound,
    Portal,
    PortalUnavailable,
    content_disposition_name,
    prepare_work_directory,
    safe_name,
    validate_signature,
    validate_url,
)


@pytest.fixture
def row():
    return DocRow(
        doc_type=DocType.OTHER_DOCUMENTS,
        doc_id="102674",
        title="Board Order",
        security="Public",
        extension=".pdf",
        position=0,
    )


def test_rejects_error_page_disguised_as_pdf(tmp_path):
    path = tmp_path / "102674.pdf"
    path.write_bytes(b"<!DOCTYPE html><html>Temporarily unavailable</html>")
    with pytest.raises(FileRejected, match="HTML"):
        validate_signature(path, path.name)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/fmi/webd/APP/connector/1/dl/file.pdf",
        "https://uarb.novascotia.ca.attacker.test/fmi/webd/APP/connector/x",
        "http://uarb.novascotia.ca/fmi/webd/APP/connector/x",
        "https://uarb.novascotia.ca/private",
        "https://user@uarb.novascotia.ca/fmi/webd/APP/connector/x",
    ],
)
def test_download_location_is_restricted_to_observed_portal_origin(url):
    with pytest.raises(FileRejected):
        validate_url(url)


def test_preserves_encoded_download_filename():
    assert (
        content_disposition_name("attachment; filename*=UTF-8''Board%20Order.pdf")
        == "Board Order.pdf"
    )
    assert safe_name("../../unsafe\\name.pdf") == "name.pdf"


def test_checkpoints_cannot_be_reused_for_a_different_matter(tmp_path):
    prepare_work_directory(tmp_path, FetchRequest(matter="M12205", doc_type=DocType.EXHIBITS))
    with pytest.raises(PortalUnavailable, match="different matter"):
        prepare_work_directory(tmp_path, FetchRequest(matter="M10431", doc_type=DocType.EXHIBITS))


async def test_low_disk_space_stops_before_navigation(tmp_path, monkeypatch):
    monkeypatch.setattr("docket.portal.shutil.disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(OSError, match="free space"):
        await Portal().fetch(FetchRequest(matter="M12205", doc_type=DocType.EXHIBITS), tmp_path)


def test_checkpoint_rejects_tampered_file_and_changed_metadata(tmp_path, row):
    path = tmp_path / "102674.pdf"
    content = b"%PDF-1.7\noriginal"
    path.write_bytes(content)
    file = FetchedFile(
        row=row,
        path=path,
        served_name=path.name,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_type="application/pdf",
    )
    portal = Portal()
    portal._checkpoint(tmp_path, row, [file])
    assert portal._restore(tmp_path, row) == [file]
    assert portal._restore(tmp_path, row.model_copy(update={"title": "Changed order"})) == []
    path.write_bytes(b"%PDF-1.7\nmodified")
    assert portal._restore(tmp_path, row) == []


async def test_chunked_download_stops_at_cap_and_removes_partial(tmp_path, row, monkeypatch):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"%PDF-1.7\n" + b"x" * 70_000

    def respond(request):
        return httpx.Response(
            200,
            headers={"content-disposition": "attachment; filename=102674.pdf"},
            stream=Chunks(),
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "docket.portal.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    portal = Portal(file_cap=1024)
    target = tmp_path / "102674.pdf"
    with pytest.raises(FileRejected, match="budget"):
        await portal._stream(
            "https://uarb.novascotia.ca/fmi/webd/APP/connector/1/dl/102674.pdf",
            {},
            target,
            row,
            "102674.pdf",
        )
    assert not target.exists()
    assert not target.with_suffix(".pdf.part").exists()


async def test_correct_pdf_with_wrong_server_filename_is_rejected(tmp_path, row, monkeypatch):
    def respond(request):
        return httpx.Response(
            200,
            headers={"content-disposition": "attachment; filename=999999.pdf"},
            content=b"%PDF-1.7\nwrong document",
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "docket.portal.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(FileRejected, match="999999"):
        await Portal()._stream(
            "https://uarb.novascotia.ca/fmi/webd/APP/connector/1/dl/102674.pdf",
            {},
            tmp_path / "102674.pdf",
            row,
            "102674.pdf",
        )


async def test_browser_launch_failure_stops_playwright_driver(monkeypatch):
    driver = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(side_effect=OSError("launch"))), stop=AsyncMock()
    )
    monkeypatch.setattr(
        "docket.portal.async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=driver)),
    )
    with pytest.raises(OSError):
        async with Portal():
            pass
    driver.stop.assert_awaited_once()


async def test_browser_close_failure_still_stops_driver():
    portal = Portal()
    portal._browser = SimpleNamespace(close=AsyncMock(side_effect=OSError("close")))
    portal._playwright = SimpleNamespace(stop=AsyncMock())
    with pytest.raises(OSError):
        await portal.__aexit__()
    portal._playwright.stop.assert_awaited_once()


@pytest.fixture
def inspected_portal():
    page = SimpleNamespace(set_default_timeout=Mock(), screenshot=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    portal = Portal()
    portal._browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    portal._lookup = AsyncMock(return_value=True)
    portal._metadata = AsyncMock(
        return_value=MatterMeta(
            matter="M12205", title="Verified matter", counts={kind: 0 for kind in DocType}
        )
    )
    portal._collect = AsyncMock()
    return portal, page, context


async def test_inspection_reads_metadata_without_selecting_a_category(inspected_portal):
    portal, page, context = inspected_portal
    meta = await portal.inspect("M12205")
    assert meta.matter == "M12205" and set(meta.counts) == set(DocType)
    portal._lookup.assert_awaited_once_with(page, "M12205")
    portal._collect.assert_not_awaited()
    context.close.assert_awaited_once()


async def test_not_found_is_confirmed_by_two_searches(inspected_portal):
    portal, _, context = inspected_portal
    portal._lookup.return_value = False
    with pytest.raises(MatterNotFound):
        await portal.inspect("M12205")
    assert portal._browser.new_context.await_count == 2
    assert context.close.await_count == 2
    portal._metadata.assert_not_awaited()


async def test_transient_failure_is_returned_without_hidden_retry(inspected_portal):
    portal, _, context = inspected_portal
    portal._lookup.side_effect = OSError("network")
    with pytest.raises(PortalUnavailable):
        await portal.inspect("M12205")
    assert portal._lookup.await_count == 1
    context.close.assert_awaited_once()


async def test_empty_fetch_preserves_inspected_counts(inspected_portal, tmp_path):
    portal, _, _ = inspected_portal
    request = FetchRequest(matter="M12205", doc_type=DocType.TRANSCRIPTS)
    result = await portal.fetch(request, tmp_path)
    assert result.request == request and result.meta == portal._metadata.return_value
    assert result.tab.all_rows_read and not result.tab.files
    portal._collect.assert_not_awaited()


async def test_invalid_inspection_never_opens_a_page(inspected_portal):
    portal, _, _ = inspected_portal
    with pytest.raises(ValueError):
        await portal.inspect("M12205/other")
    portal._browser.new_context.assert_not_awaited()


async def test_failed_sibling_preserves_verified_files(tmp_path, row):
    active = SimpleNamespace(wait_for=AsyncMock(), count=AsyncMock(return_value=1))
    locator = SimpleNamespace(
        click=AsyncMock(),
        locator=Mock(return_value=active),
        get_by_role=Mock(return_value=SimpleNamespace(click=AsyncMock())),
    )
    buttons = SimpleNamespace(
        first=SimpleNamespace(wait_for=AsyncMock()),
        all_text_contents=AsyncMock(return_value=["102674.pdf", "102674.pdf"]),
        nth=Mock(return_value=SimpleNamespace(click=AsyncMock())),
    )
    export = SimpleNamespace(count=AsyncMock(return_value=0))
    windows = SimpleNamespace(filter=Mock(return_value=export))
    downloads = [
        SimpleNamespace(
            url="https://uarb.novascotia.ca/fmi/webd/APP/connector/1/dl/102674.pdf",
            suggested_filename="102674.pdf",
            cancel=AsyncMock(),
            delete=AsyncMock(),
        )
        for _ in range(2)
    ]
    pending = []
    for download in downloads:
        value = asyncio.get_running_loop().create_future()
        value.set_result(download)
        manager = MagicMock()
        manager.__aenter__.return_value = SimpleNamespace(value=value)
        pending.append(manager)
    page = SimpleNamespace(
        locator=Mock(side_effect=lambda selector: windows if selector == ".v-window" else buttons),
        wait_for_function=AsyncMock(),
        expect_download=Mock(side_effect=pending),
        context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
    )
    file = FetchedFile(
        row=row,
        path=tmp_path / "original.pdf",
        served_name="102674.pdf",
        size=12,
        sha256="verified-by-stream",
        content_type="application/pdf",
    )
    portal = Portal()
    portal._row_locator = AsyncMock(return_value=locator)
    portal._stream = AsyncMock(side_effect=[file, FileRejected("invalid payload")])
    files, skipped = await portal._download_row(page, row, tmp_path, 10)
    assert files == [file]
    assert len(skipped) == 1 and "invalid payload" in skipped[0].detail
    for download in downloads:
        download.delete.assert_awaited_once()


async def test_unnumbered_export_replaces_comma_name_and_preserves_extension(tmp_path):
    row = DocRow(
        doc_type=DocType.RECORDINGS,
        doc_id="row-1",
        title="Hearing recording",
        security="Public",
        position=0,
        has_portal_id=False,
    )
    safe_export = "Recordings_row-1.wav"
    target = tmp_path / f"Recordings_1_1_{safe_export}"
    active = SimpleNamespace(wait_for=AsyncMock(), count=AsyncMock(return_value=1))
    locator = SimpleNamespace(
        click=AsyncMock(),
        locator=Mock(return_value=active),
        get_by_role=Mock(return_value=SimpleNamespace(click=AsyncMock())),
    )
    export_name = SimpleNamespace(
        input_value=AsyncMock(return_value="../../October 6, 2014, AM, Part 1.WAV"),
        fill=AsyncMock(),
    )
    confirm = SimpleNamespace(click=AsyncMock())
    export = SimpleNamespace(
        count=AsyncMock(return_value=1),
        locator=Mock(return_value=export_name),
        get_by_role=Mock(return_value=confirm),
    )
    windows = SimpleNamespace(filter=Mock(return_value=export))
    buttons = SimpleNamespace(
        first=SimpleNamespace(wait_for=AsyncMock()),
        all_text_contents=AsyncMock(return_value=[safe_export]),
        nth=Mock(return_value=SimpleNamespace(click=AsyncMock())),
    )
    download = SimpleNamespace(
        url=f"https://uarb.novascotia.ca/fmi/webd/APP/connector/1/dl/{safe_export}",
        suggested_filename=safe_export,
        cancel=AsyncMock(),
        delete=AsyncMock(),
    )
    value = asyncio.get_running_loop().create_future()
    value.set_result(download)
    pending = MagicMock()
    pending.__aenter__.return_value = SimpleNamespace(value=value)
    page = SimpleNamespace(
        locator=Mock(side_effect=lambda selector: windows if selector == ".v-window" else buttons),
        wait_for_function=AsyncMock(),
        expect_download=Mock(return_value=pending),
        context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
    )
    file = FetchedFile(
        row=row,
        path=target,
        served_name=safe_export,
        size=12,
        sha256="verified-by-stream",
        content_type="audio/wav",
    )
    portal = Portal()
    portal._row_locator = AsyncMock(return_value=locator)
    portal._stream = AsyncMock(return_value=file)
    files, skipped = await portal._download_row(page, row, tmp_path, 1)
    export_name.fill.assert_awaited_once_with(safe_export)
    export.get_by_role.assert_called_once_with("button", name="OK", exact=True)
    confirm.click.assert_awaited_once()
    portal._stream.assert_awaited_once_with(download.url, {}, target, row, safe_export)
    assert files == [file] and skipped == []
    download.cancel.assert_awaited_once()
    download.delete.assert_awaited_once()


@pytest.mark.parametrize(
    "category,security,omission",
    [
        (DocType.RECORDINGS, "", None),
        (DocType.RECORDINGS, "Confidential", "not_public"),
        (DocType.OTHER_DOCUMENTS, "", "access_unlabelled"),
    ],
)
async def test_collection_limits_unlabelled_access_to_recordings(
    tmp_path, row, category, security, omission
):
    row = row.model_copy(update={"doc_type": category, "security": security})
    result = FetchResult(
        request=FetchRequest(matter="M10431", doc_type=category, limit=1),
        meta=MatterMeta(
            matter="M10431",
            title="Hearing",
            counts={kind: int(kind == category) for kind in DocType},
        ),
        tab=TabResult(doc_type=category, count=1),
    )
    file = FetchedFile(
        row=row,
        path=tmp_path / "recording.wav",
        served_name="recording.wav",
        size=12,
        sha256="verified-by-stream",
        content_type="audio/wav",
    )
    page = SimpleNamespace(
        get_by_role=Mock(return_value=SimpleNamespace(click=AsyncMock())),
        wait_for_function=AsyncMock(),
    )
    portal = Portal()
    portal._rows = AsyncMock(return_value=[row])
    portal._restore = Mock(return_value=[])
    portal._download_row = AsyncMock(return_value=([file], []))
    portal._checkpoint = Mock()
    portal._dismiss = AsyncMock()
    await portal._collect(page, result, tmp_path)
    assert result.tab.all_rows_read
    assert result.tab.rows_read[0].security == security
    if omission:
        portal._download_row.assert_not_awaited()
        assert result.tab.files == []
        assert [item.reason for item in result.tab.skipped] == [omission]
    else:
        portal._download_row.assert_awaited_once_with(page, row, tmp_path, 1)
        assert result.tab.files == [file]
        assert result.tab.skipped == []
