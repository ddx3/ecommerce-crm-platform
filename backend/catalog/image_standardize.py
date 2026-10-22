"""Bringing product photos to the storefront standard.

Standard: a 1080×1080 square, the object centered on a white background with small margins.

Two modes (chosen automatically):
  • Background is already clean white → do NOT touch the object, just crop the white margins
    and center it on the square. No ML — the photo stays as is (only the size changes).
  • Background is mottled/dark → cut the object out with rembg (local u2net model) and
    center it on white. If no object is found — fall back to centering the original.

Previously rembg ran ALWAYS, and on already-clean photos the model «ate» the object's edges /
made holes. Now clean photos take the fast path without ML.
"""
from io import BytesIO

import numpy as np
from PIL import Image, ImageOps

CANVAS = 1080
MARGIN_RATIO = 0.06  # margin around the product, as a fraction of the square's side

# Threshold for an «almost white» pixel (all channels ≥). Conservative — so as not to cut off
# the light edges of the product itself: better to leave a bit more margin than to bite into the
# object.
WHITE_THRESH = 248
# Fraction of almost-white pixels along the perimeter at which the background is considered clean
# white.
CLEAN_BG_FRACTION = 0.92

_session = None


def _get_session():
    """Cached rembg session — the model is loaded once per process."""
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session("u2net")
    return _session


def _is_clean_white_bg(rgb):
    """True if the photo's background is already uniformly white (the object need not be cut out)."""
    arr = np.asarray(rgb, dtype=np.uint8)
    h, w = arr.shape[:2]
    b = max(2, round(min(h, w) * 0.04))  # border along the perimeter ~4% of the side
    border = np.concatenate([
        arr[:b].reshape(-1, 3), arr[-b:].reshape(-1, 3),
        arr[:, :b].reshape(-1, 3), arr[:, -b:].reshape(-1, 3),
    ])
    near_white = np.all(border >= WHITE_THRESH, axis=1).mean()
    return near_white >= CLEAN_BG_FRACTION


def _trim_white_bbox(rgb):
    """BBox of the region that is NOT almost white. None if everything is white."""
    arr = np.asarray(rgb, dtype=np.uint8)
    nonwhite = np.any(arr < WHITE_THRESH, axis=2)
    ys, xs = np.where(nonwhite)
    if len(xs) == 0:
        return None
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _cutout(src_rgba):
    """RGBA original → RGBA with the background removed, cropped to the object's outline.

    Returns None if no object is found (empty alpha) — the caller must fall back.
    We do NOT catch ImportError from rembg: it is an environment problem the admin
    needs to find out about.
    """
    from rembg import remove
    try:
        result = remove(src_rgba, session=_get_session())
    except Exception:
        return None
    alpha = result.getchannel("A")
    bbox = alpha.getbbox()
    if not bbox:
        return None
    return result.crop(bbox)


def _paste_centered(obj, mask=None):
    """Scales obj to fit the inner square and pastes it centered on a white CANVAS×CANVAS background."""
    inner = CANVAS - 2 * round(CANVAS * MARGIN_RATIO)
    w, h = obj.size
    scale = min(inner / w, inner / h)
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    resized = obj.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    x = (CANVAS - nw) // 2
    y = (CANVAS - nh) // 2
    if mask is not None:
        canvas.paste(resized, (x, y), mask.resize((nw, nh), Image.LANCZOS))
    else:
        canvas.paste(resized, (x, y))
    return canvas


def standardize(data: bytes) -> bytes:
    """Photo bytes in any format → 1080×1080 JPEG bytes on a white background."""
    src = Image.open(BytesIO(data))
    src = ImageOps.exif_transpose(src)  # respect EXIF orientation (phone photos)

    rgb = src.convert("RGB")
    if _is_clean_white_bg(rgb):
        # Background is already clean — size only: crop the white margins and center.
        bbox = _trim_white_bbox(rgb)
        obj = rgb.crop(bbox) if bbox else rgb
        result = _paste_centered(obj)
    else:
        # Mottled/dark background — cut out the object.
        trimmed = _cutout(src.convert("RGBA"))
        if trimmed is None:
            result = _paste_centered(rgb)  # failed — center the original
        else:
            result = _paste_centered(trimmed.convert("RGB"), mask=trimmed.getchannel("A"))

    out = BytesIO()
    result.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()
