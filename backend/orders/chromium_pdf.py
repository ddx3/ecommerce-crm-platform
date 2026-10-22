"""HTML→PDF via Chromium (Playwright).

Called from admin_pdf.py in a SEPARATE process (subprocess), not within Django:
- Playwright sync/async conflicts with the Django event loop under ASGI (uvicorn/daphne);
- on Windows asyncio needs ProactorEventLoop for subprocess.
A separate process has a clean main thread and its own loop — no conflicts.

The script deliberately does NOT import Django: input is a ready HTML file, output is a PDF.

Two modes:
    <html_file> <pdf_file>          — order receipt: a single sheet sized to the content height
    --manifest <manifest.json>      — batch of instructions: real A4 with margins

Manifest: {"jobs": [{"html": "<abs>", "pdf": "<abs>"}, ...]}
The batch is rendered in ONE browser launch: a cold start costs ~2.4 s, and each subsequent sheet
hundreds of milliseconds, so a separate process per instruction would turn an export of 50 orders
into a two-minute wait.
"""
import asyncio
import sys


# PDF width = receipt card width (max-width 560px in the template), no margins around.
# Height is computed from the content — the PDF is exactly the receipt, edge to edge.
PAGE_WIDTH = 560


async def _render(html: str, pdf_path: str) -> None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        # --no-sandbox — mandatory for Chromium running as root in Docker.
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        try:
            # viewport.height is deliberately small: otherwise scrollHeight for a short receipt
            # would hit the viewport height and leave empty space at the bottom.
            page = await browser.new_page(viewport={'width': PAGE_WIDTH, 'height': 100})
            await page.set_content(html, wait_until='load')
            await page.emulate_media(media='print')
            # Page height = actual content height (+ a margin so the last line doesn't spill onto an
            # extra page). One sheet exactly sized to the receipt.
            height = await page.evaluate(
                'Math.ceil(document.body.scrollHeight) + 4'
            )
            await page.pdf(
                path=pdf_path,
                width=f'{PAGE_WIDTH}px',
                height=f'{height}px',
                print_background=True,
                margin={'top': '0', 'bottom': '0', 'left': '0', 'right': '0'},
            )
        finally:
            await browser.close()


async def _render_batch(jobs: list) -> None:
    """Render a list of HTML files to A4 PDFs in a single browser launch."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=['--no-sandbox'])
        try:
            page = await browser.new_page()
            for job in jobs:
                # goto file://, not set_content: otherwise the document has no base URL
                # and relative /media/... paths don't resolve.
                url = 'file:///' + job['html'].replace('\\', '/').lstrip('/')
                await page.goto(url, wait_until='load')
                await page.emulate_media(media='print')
                await page.pdf(
                    path=job['pdf'],
                    format='A4',                 # NOT width/height: only format gives
                    print_background=True,       # real per-page pagination
                    prefer_css_page_size=False,
                    # No headers/footers: the sheet goes to the customer in the box, and service
                    # marks (order number, surname, "sheet 1 of 2") are unnecessary on
                    # it. Margins are set HERE, not in CSS — an @page margin in the template would
                    # override these values and the content would sit right against the edge.
                    margin={'top': '11mm', 'bottom': '11mm',
                            'left': '12mm', 'right': '12mm'},
                )
        finally:
            await browser.close()


def _run(coro):
    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def render_manifest(manifest_path: str) -> None:
    import json
    with open(manifest_path, encoding='utf-8') as fh:
        data = json.load(fh)
    _run(_render_batch(data.get('jobs') or []))


def html_to_pdf(html: str, pdf_path: str) -> None:
    if sys.platform == 'win32':
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_render(html, pdf_path))
    finally:
        loop.close()


if __name__ == '__main__':
    if sys.argv[1] == '--manifest':
        render_manifest(sys.argv[2])
    else:
        html_file, pdf_file = sys.argv[1], sys.argv[2]
        with open(html_file, encoding='utf-8') as fh:
            html_to_pdf(fh.read(), pdf_file)
