"""Bounded, serial retrieval from the public FileMaker WebDirect portal.

The selectors below were checked against the live portal. A row exists before its
data arrives, so navigation waits for both the selected layout and populated cells.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from playwright.async_api import Browser, Page, async_playwright
from playwright.async_api import Error as BrowserError

from .files import atomic_write, digest
from .models import (
    PORTAL_URL,
    DocRow,
    DocType,
    FetchedFile,
    FetchRequest,
    FetchResult,
    MatterMeta,
    Skipped,
    TabResult,
)


class PortalUnavailable(RuntimeError):
    """The source could not be read reliably; this is not a zero result."""


class MatterNotFound(RuntimeError):
    """Two independent searches explicitly returned no matching matter."""


class FileRejected(RuntimeError):
    pass


_ID_FIELD = {DocType.EXHIBITS: 300, DocType.KEY_DOCUMENTS: 268}
_EXT_FIELD = {DocType.EXHIBITS: 314, DocType.KEY_DOCUMENTS: 284}
_UNNUMBERED = {DocType.TRANSCRIPTS, DocType.RECORDINGS}
_ROW = "tr.v-grid-row-has-data"
log = logging.getLogger(__name__)


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name.replace("\\", "/")).name)
    return name.strip("._")[:160] or "document"


def validate_url(url: str) -> None:
    target = urlsplit(url)
    source = urlsplit(PORTAL_URL)
    if (
        target.scheme != source.scheme
        or target.netloc != source.netloc
        or not target.path.startswith("/fmi/webd/APP/connector/")
        or target.username
        or target.password
    ):
        raise FileRejected("The portal returned an unexpected download location.")


def validate_signature(path: Path, filename: str) -> str:
    with path.open("rb") as handle:
        head = handle.read(1024)
        at_eof = not handle.read(1)
    suffix = Path(filename).suffix.lower()
    if not head:
        raise FileRejected("The downloaded file was empty.")
    if head.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise FileRejected("The download contained an HTML error page.")
    if suffix == ".pdf" and b"%PDF-" in head[:1024]:
        return "application/pdf"
    if suffix in {".docx", ".xlsx", ".pptx", ".zip"} and head.startswith(b"PK\x03\x04"):
        return "application/zip"
    if suffix in {".doc", ".xls", ".ppt"} and head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/vnd.ms-office"
    if suffix == ".wav" and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if suffix == ".mp3" and (
        head.startswith(b"ID3") or (head[0] == 255 and head[1:2] and head[1] & 224 == 224)
    ):
        return "audio/mpeg"
    if suffix in {".m4a", ".mp4"} and head[4:8] == b"ftyp":
        return "audio/mp4" if suffix == ".m4a" else "video/mp4"
    if suffix in {".ogg", ".oga"} and head.startswith(b"OggS"):
        return "audio/ogg"
    if suffix == ".wma" and head.startswith(bytes.fromhex("3026b2758e66cf11")):
        return "audio/x-ms-wma"
    if suffix in {".txt", ".csv"} and b"\x00" not in head:
        try:
            codecs.getincrementaldecoder("utf-8-sig")().decode(head, final=at_eof)
        except UnicodeDecodeError as exc:
            raise FileRejected("The text file encoding could not be verified.") from exc
        return "text/plain" if suffix == ".txt" else "text/csv"
    raise FileRejected(f"The file signature did not match a supported {suffix or 'file'} format.")


def content_disposition_name(header: str) -> str | None:
    message = Message()
    message["Content-Disposition"] = header
    value = message.get_filename()
    return unquote(value) if value else None


def prepare_work_directory(directory: Path, request: FetchRequest) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    identity = {"matter": request.matter, "doc_type": request.doc_type.value}
    marker = directory / "request.json"
    if marker.exists() and json.loads(marker.read_text()) != identity:
        raise PortalUnavailable("This work directory belongs to a different matter or category.")
    atomic_write(marker, json.dumps(identity).encode())


class Portal:
    def __init__(
        self,
        headless: bool = True,
        file_cap: int = 18_000_000,
        byte_budget: int = 60_000_000,
        timeout_seconds: int = 180,
    ):
        self.headless = headless
        self.file_cap = file_cap
        self.byte_budget = byte_budget
        self.timeout_seconds = timeout_seconds
        self._browser: Browser | None = None
        self._playwright = None
        self._lock = asyncio.Lock()
        self._bytes = 0

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        options = {"headless": self.headless}
        if executable := os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
            options["executable_path"] = executable
        try:
            self._browser = await self._playwright.chromium.launch(**options)
        except BaseException:
            await self._playwright.stop()
            raise
        return self

    async def __aexit__(self, *_):
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._playwright:
                await self._playwright.stop()

    @contextlib.asynccontextmanager
    async def _matter_page(self, matter: str, work_dir: Path | None = None):
        if not self._browser:
            raise RuntimeError("Use Portal as an async context manager.")
        if not re.fullmatch(r"M[0-9]{5}", matter):
            raise ValueError("Expected a matter number such as M12205.")
        async with self._lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    for attempt in range(2):
                        context = await self._browser.new_context(
                            viewport={"width": 1600, "height": 2400}, accept_downloads=True
                        )
                        page = None
                        try:
                            page = await context.new_page()
                            page.set_default_timeout(15_000)
                            if not await self._lookup(page, matter):
                                if attempt == 0:
                                    continue
                                raise MatterNotFound(
                                    f"{matter} was not found in two independent portal searches."
                                )
                            yield page, await self._metadata(page, matter)
                            return
                        except Exception:
                            if work_dir is not None and page is not None:
                                with contextlib.suppress(BrowserError):
                                    await page.screenshot(
                                        path=str(work_dir / "portal-failure.png"), full_page=True
                                    )
                            raise
                        finally:
                            await context.close()
            except MatterNotFound:
                raise
            except TimeoutError as exc:
                raise PortalUnavailable(
                    "The portal job exceeded its time limit; completed files were checkpointed."
                ) from exc
            except PortalUnavailable:
                raise
            except Exception as exc:
                raise PortalUnavailable(
                    f"The portal could not be read reliably: {type(exc).__name__}: {exc}"
                ) from exc

    async def inspect(self, matter_number: str) -> MatterMeta:
        async with self._matter_page(matter_number) as (_, meta):
            return meta

    async def fetch(self, request: FetchRequest, work_dir: Path) -> FetchResult:
        prepare_work_directory(work_dir, request)
        # Leave space for originals, ZIPs, encoded MIME and subsequent ledger writes.
        if shutil.disk_usage(work_dir).free < 512_000_000 + 4 * self.byte_budget:
            raise OSError("Insufficient free space for document retrieval")
        started = time.monotonic()
        async with self._matter_page(request.matter, work_dir) as (page, meta):
            self._bytes = 0
            lookup_time = time.monotonic() - started
            result = FetchResult(
                request=request,
                meta=meta,
                tab=TabResult(doc_type=request.doc_type, count=meta.counts[request.doc_type]),
            )
            if result.tab.count:
                await self._collect(page, result, work_dir)
            else:
                result.tab.all_rows_read = True
            result.timings = {
                "lookup_seconds": round(lookup_time, 3),
                "portal_seconds": round(time.monotonic() - started, 3),
            }
            return result

    async def _lookup(self, page: Page, matter: str) -> bool:
        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45_000)
        field = page.get_by_text("eg M01234", exact=True).locator("..").locator(".text")
        await field.wait_for()
        box = await field.bounding_box()
        await field.click()
        editor = page.locator('[contenteditable="true"]')
        await editor.wait_for()
        await editor.fill(matter)
        if (await editor.inner_text()).strip() != matter:
            raise PortalUnavailable("The search field did not retain the requested matter number.")
        await editor.press("Tab")
        buttons = page.get_by_role("button", name="Search", exact=True)
        positions = await buttons.evaluate_all("els => els.map(e => e.getBoundingClientRect().y)")
        index = min(range(len(positions)), key=lambda i: abs(positions[i] - box["y"]))
        await buttons.nth(index).click()
        await page.wait_for_function(
            """matter => {
              const id = document.querySelector('.fm_object_286 .text');
              const text = document.body.innerText;
              return (id && id.innerText.trim() === matter && /Exhibits -\\s*\\d/.test(text))
                || /No Records Found|No records matched/i.test(text);
            }""",
            arg=matter,
            timeout=30_000,
        )
        return not bool(
            re.search(
                r"No Records Found|No records matched",
                await page.locator("body").inner_text(),
                re.IGNORECASE,
            )
        )

    async def _metadata(self, page: Page, matter: str) -> MatterMeta:
        # Read the original matter layout: its second date is labelled Decision Date.
        # The document layouts relabel it Date Final Submissions; do not conflate them.
        await page.wait_for_function(
            "document.querySelector('.fm_object_286 .text')?.innerText.trim() !== ''"
        )
        fields = await page.evaluate(
            """() => {
              const value = n => document.querySelector('.fm_object_' + n)?.innerText.trim() || '';
              const label = document.querySelector('[id="b0p0o290i0i0r1"]');
              return {matter:value(286),type:value(287),status:value(289),title:label?.innerText.trim() || '',
                date_received:value(292),second_date:value(294),outcome:value(295),category:value(298),
                second_date_label:document.querySelector('.fm_object_310')?.innerText.trim() || ''};
            }"""
        )
        if fields["matter"] != matter or not fields["title"]:
            raise PortalUnavailable("The portal returned an incomplete or different matter.")
        counts = {}
        for category in DocType:
            label = await page.get_by_role(
                "button", name=re.compile(rf"^{re.escape(category.value)} -")
            ).inner_text()
            match = re.fullmatch(rf"{re.escape(category.value)} -\s*([\d,]+)", label.strip())
            if not match:
                raise PortalUnavailable(f"Could not read the {category.value} count.")
            counts[category] = int(match[1].replace(",", ""))
        return MatterMeta(**fields, counts=counts)

    async def _rows(self, page: Page, category: DocType) -> list[DocRow]:
        id_field = _ID_FIELD.get(category, 201)
        ext_field = _EXT_FIELD.get(category, 308)
        raw = await page.locator(_ROW).evaluate_all(
            """(rows, fields) => rows.map(row => {
              const text = n => Array.from(row.querySelectorAll('.fm_object_'+n+' .text')).map(e=>e.innerText.trim()).filter(Boolean);
              const rect = row.getBoundingClientRect();
              return {id:text(fields[0])[0] || '',title:text(202).join(' | '),filed:text(203)[0] || '',
                security:text(204)[0] || '',extension:text(fields[1])[0] || '',
                position:Math.round(new DOMMatrix(getComputedStyle(row).transform).m42/rect.height),
                ready:!!row.querySelector('button')};
            }).filter(row=>row.ready && row.id && row.title)""",
            [id_field, ext_field],
        )
        rows = []
        for item in raw:
            identifier = item.pop("id")
            item.pop("ready")
            numbered = category not in _UNNUMBERED
            if category in _UNNUMBERED:
                item["filed"] = identifier
                identifier = f"row-{item['position'] + 1}-{identifier.replace('/', '-')}"
            rows.append(
                DocRow(doc_type=category, doc_id=identifier, has_portal_id=numbered, **item)
            )
        return sorted(rows, key=lambda row: row.position)

    async def _collect(self, page: Page, result: FetchResult, directory: Path):
        category = result.request.doc_type
        await page.get_by_role(
            "button", name=re.compile(rf"^{re.escape(category.value)} -\s*[\d,]+$")
        ).click()
        await page.wait_for_function(
            "count => document.querySelector('table[role=grid]')?.getAttribute('aria-rowcount') === String(count)",
            arg=result.tab.count,
        )
        await page.wait_for_function(
            """idField => Array.from(document.querySelectorAll('tr.v-grid-row-has-data')).some(row => {
                const text = n => Array.from(row.querySelectorAll('.fm_object_'+n+' .text'))
                    .some(e => e.innerText.trim());
                return text(idField) && text(202) && row.querySelector('button');
            })""",
            arg=_ID_FIELD.get(category, 201),
        )
        seen: set[int] = set()
        for _ in range(40):  # Bounded even if the virtual grid stops advancing.
            rows = await self._rows(page, category)
            new = [row for row in rows if row.position not in seen]
            if not new:
                break
            for row in new:
                seen.add(row.position)
                result.tab.rows_read.append(row)
                if not row.downloadable:
                    result.tab.skipped.append(
                        Skipped(
                            row=row,
                            reason="not_public" if row.security.strip() else "access_unlabelled",
                            detail=f"Access label: {row.security or 'unspecified'}",
                        )
                    )
                    continue
                if self._bytes >= self.byte_budget:
                    result.tab.skipped.append(
                        Skipped(
                            row=row,
                            reason="byte_budget",
                            detail="The request download budget was exhausted.",
                        )
                    )
                    return
                remaining = result.request.limit - len(result.tab.files)
                restored = self._restore(directory, row)
                if restored:
                    result.tab.files.extend(restored[:remaining])
                    self._bytes += sum(file.size for file in restored[:remaining])
                else:
                    try:
                        log.info("portal download %s %s", row.doc_type.value, row.doc_id)
                        files, omissions = await self._download_row(page, row, directory, remaining)
                        result.tab.files.extend(files)
                        result.tab.skipped.extend(omissions)
                        if not omissions:
                            self._checkpoint(directory, row, files)
                    except (FileRejected, httpx.HTTPError, BrowserError, RuntimeError) as exc:
                        log.warning("portal skipped %s: %s", row.doc_id, exc)
                        result.tab.skipped.append(
                            Skipped(row=row, reason="download_failed", detail=str(exc)[:300])
                        )
                    finally:
                        await self._dismiss(page)
                if len(result.tab.files) >= result.request.limit:
                    result.tab.all_rows_read = len(seen) >= result.tab.count
                    return
            if len(seen) >= result.tab.count:
                result.tab.all_rows_read = True
                return
            before = sorted(seen)
            moved = await page.locator(".v-grid-scroller-vertical").evaluate(
                "e => {const old=e.scrollTop; e.scrollTop += Math.max(68,e.clientHeight-68); return e.scrollTop>old;}"
            )
            if not moved:
                break
            try:
                await page.wait_for_function(
                    """seen => Array.from(document.querySelectorAll('tr.v-grid-row-has-data')).some(row=>{
                      const pos=Math.round(new DOMMatrix(getComputedStyle(row).transform).m42/row.getBoundingClientRect().height);
                      return !seen.includes(pos) && row.querySelector('.fm_object_202 .text')?.innerText.trim();
                    })""",
                    arg=before,
                    timeout=10_000,
                )
            except BrowserError:
                break
        result.tab.all_rows_read = len(seen) >= result.tab.count

    async def _row_locator(self, page: Page, row: DocRow):
        positions = await page.locator(_ROW).evaluate_all(
            "rows=>rows.map(row=>Math.round(new DOMMatrix(getComputedStyle(row).transform).m42/row.getBoundingClientRect().height))"
        )
        if row.position not in positions:
            raise FileRejected("The selected document is no longer visible.")
        locator = page.locator(_ROW).nth(positions.index(row.position))
        current = next(
            (
                item
                for item in await self._rows(page, row.doc_type)
                if item.position == row.position
            ),
            None,
        )
        if current != row:
            raise FileRejected("The selected document changed before download.")
        return locator

    async def _download_row(
        self, page: Page, row: DocRow, directory: Path, limit: int
    ) -> tuple[list[FetchedFile], list[Skipped]]:
        locator = await self._row_locator(page, row)
        # Click the row's title region, including the readonly glass pane used by
        # Exhibits. Clicking the underlying readonly text itself is intercepted.
        await locator.click(position={"x": 200, "y": 10})
        await locator.locator(".iwps_body_active").wait_for(state="attached")
        await locator.get_by_role("button", name="GO GET IT", exact=True).click()
        await page.wait_for_function(
            "Array.from(document.querySelectorAll('.v-window')).some(e=>/Download Files|Export Field to File/.test(e.innerText))"
        )
        active = await locator.locator(".iwps_body_active").count()
        if not active:
            raise FileRejected("The portal did not confirm the selected document row.")
        export = page.locator(".v-window").filter(has_text="Export Field to File")
        if await export.count():
            original = await export.locator("input").input_value()
            suffix = Path(original).suffix.lower()
            if not suffix:
                raise FileRejected("The export has no identifiable file type.")
            # Export names can contain commas that Chromium interprets as malformed headers.
            await export.locator("input").fill(
                safe_name(f"{row.doc_type.value}_{row.doc_id}{suffix}")
            )
            await export.get_by_role("button", name="OK", exact=True).click()
        await page.locator(".fm-download-button").first.wait_for()
        names = [
            name.strip() for name in await page.locator(".fm-download-button").all_text_contents()
        ]
        files = []
        skipped = []
        for index, name in enumerate(names):
            if len(files) >= limit:
                break
            if self._bytes >= self.byte_budget:
                skipped.append(
                    Skipped(row=row, reason="byte_budget", detail="Download budget reached.")
                )
                break
            download = None
            try:
                if row.has_portal_id and Path(name).stem != row.doc_id:
                    raise FileRejected(
                        f"Requested document {row.doc_id}, but the portal offered {name}."
                    )
                button = page.locator(".fm-download-button").nth(index)
                async with page.expect_download(timeout=30_000) as pending:
                    await button.click()
                download = await pending.value
                url = download.url
                offered = download.suggested_filename
                await download.cancel()
                validate_url(url)
                if offered not in {name, "download"}:
                    raise FileRejected(f"The download filename changed from {name} to {offered}.")
                filename = safe_name(f"{row.doc_type.value}_{row.position + 1}_{index + 1}_{name}")
                target = directory / filename
                cookies = {
                    cookie["name"]: cookie["value"] for cookie in await page.context.cookies(url)
                }
                files.append(await self._stream(url, cookies, target, row, name))
            except (FileRejected, httpx.HTTPError, BrowserError, RuntimeError) as exc:
                skipped.append(
                    Skipped(row=row, reason="download_failed", detail=f"{name}: {exc}"[:300])
                )
            finally:
                if download is not None:
                    with contextlib.suppress(BrowserError):
                        await download.delete()
        return files, skipped

    async def _stream(
        self, url: str, cookies: dict, path: Path, row: DocRow, name: str
    ) -> FetchedFile:
        partial = path.with_suffix(path.suffix + ".part")
        size = 0
        sha = hashlib.sha256()
        try:
            async with (
                httpx.AsyncClient(
                    cookies=cookies, timeout=httpx.Timeout(45, connect=15), follow_redirects=False
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code != 200:
                    raise FileRejected(f"The source returned HTTP {response.status_code}.")
                served = content_disposition_name(response.headers.get("content-disposition", ""))
                if served and served != name:
                    raise FileRejected(f"The source served {served}, expected {name}.")
                declared = int(response.headers.get("content-length", 0))
                if declared > self.file_cap or declared > self.byte_budget - self._bytes:
                    raise FileRejected("The file exceeds the attachment or request size budget.")
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        self._bytes += len(chunk)
                        if size > self.file_cap or self._bytes > self.byte_budget:
                            raise FileRejected(
                                "The file exceeds the attachment or request size budget."
                            )
                        # WAV stores its whole length in the first twelve bytes.
                        if (
                            size == len(chunk)
                            and chunk[:4] == b"RIFF"
                            and chunk[8:12] == b"WAVE"
                            and int.from_bytes(chunk[4:8], "little") + 8 > self.file_cap
                        ):
                            raise FileRejected(
                                "The WAV recording exceeds the attachment size budget."
                            )
                        handle.write(chunk)
                        sha.update(chunk)
            mime = validate_signature(partial, name)
            partial.replace(path)
            return FetchedFile(
                row=row,
                path=path.resolve(),
                served_name=name,
                size=size,
                sha256=sha.hexdigest(),
                content_type=mime,
            )
        finally:
            partial.unlink(missing_ok=True)

    async def _dismiss(self, page: Page):
        for _ in range(3):
            windows = page.locator(".v-window")
            if not await windows.count():
                return
            close = windows.get_by_role("button", name="Close", exact=True)
            cancel = windows.get_by_role("button", name="Cancel", exact=True)
            if await close.count():
                await close.last.click()
            elif await cancel.count():
                await cancel.last.click()
            else:
                return
            await page.wait_for_timeout(150)

    @staticmethod
    def _checkpoint_path(directory: Path, row: DocRow) -> Path:
        identity = json.dumps(row.model_dump(mode="json"), sort_keys=True).encode()
        return directory / "checkpoints" / (hashlib.sha256(identity).hexdigest() + ".json")

    def _checkpoint(self, directory: Path, row: DocRow, files: list[FetchedFile]):
        if not files:
            return
        target = self._checkpoint_path(directory, row)
        atomic_write(target, json.dumps([file.model_dump(mode="json") for file in files]).encode())

    def _restore(self, directory: Path, row: DocRow) -> list[FetchedFile]:
        target = self._checkpoint_path(directory, row)
        try:
            files = [FetchedFile.model_validate(value) for value in json.loads(target.read_text())]
            for file in files:
                if file.row != row or not file.path.resolve().is_relative_to(directory.resolve()):
                    return []
                if (
                    file.size > self.file_cap
                    or file.path.stat().st_size != file.size
                    or digest(file.path) != file.sha256
                ):
                    return []
                validate_signature(file.path, file.served_name)
            if sum(file.size for file in files) > self.byte_budget - self._bytes:
                return []
            return files
        except (OSError, ValueError, FileRejected):
            return []
