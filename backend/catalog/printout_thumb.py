"""Thumbnail of the printout's first page — so the tile looks like a photo.

Rendered to `printouts/thumbs/<printout_pk>.png` at a fixed path: the frontend simply tries to load
it and, if the file is missing, draws a card with the file type. So a missing preview (docx, or
PyMuPDF not installed) breaks nothing.

PyMuPDF is imported softly: without it the function simply does nothing.
"""
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

THUMB_DIR = 'printouts/thumbs'
THUMB_WIDTH = 340          # ~3x the tile width (113px), so it does not look blurry on retina


def thumb_rel_path(pk) -> str:
    return f'{THUMB_DIR}/{pk}.png'


def thumb_abs_path(pk) -> str:
    return os.path.join(settings.MEDIA_ROOT, THUMB_DIR, f'{pk}.png')


def delete_thumb(pk) -> None:
    try:
        os.remove(thumb_abs_path(pk))
    except OSError:
        pass


def make_thumb(printout) -> bool:
    """Renders a preview of the sheet's first page. True if the file appeared."""
    name = printout.file.name if printout.file else ''
    if not name:
        delete_thumb(printout.pk)
        return False
    if not name.lower().endswith('.pdf'):
        delete_thumb(printout.pk)          # there are no previews for .docx
        return False
    try:
        import pymupdf                     # noqa: PLC0415 — optional dependency
    except ImportError:
        return False
    try:
        src = printout.file.path
    except (NotImplementedError, ValueError):
        return False                       # non-local storage — not our case
    if not os.path.exists(src):
        return False
    try:
        doc = pymupdf.open(src)
        try:
            page = doc[0]
            zoom = THUMB_WIDTH / max(page.rect.width, 1)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        finally:
            doc.close()
        dst = thumb_abs_path(printout.pk)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        pix.save(dst)
        return True
    except Exception:
        # We remove the old preview: otherwise, after replacing the file with a PDF that fails to
        # render (corrupt or encrypted), the tile would keep showing the sheet of the PREVIOUS file
        # — worse than an honest "no preview" card.
        delete_thumb(printout.pk)
        logger.warning('Failed to generate the printout preview for variant %s',
                       printout.pk, exc_info=True)
        return False
