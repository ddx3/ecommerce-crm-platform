"""Photos in the bundle description draft.

Quill inserts a photo into the text as a data: URI (base64) right inside the field's HTML.
Previously such
HTML went to the model as is — hundreds of thousands of base64 characters per photo — and the
response
(only text tags allowed) lost the photo. Now, before the text is parsed, each embedded
photo is classified by vision:

  photo — a meaningful illustration: stays in the description. Saved as a file in media,
          and in the HTML becomes a plain <img src="/media/...">, not base64;
  table — becomes a real <table> right in the text that the model then parses;
  text  — is transcribed into text in place of the image;
  other — logo/banner/ad: discarded.

Only notes about what did NOT work out (the photo could not be read, was not recognized,
was not placed) are returned to the manager. Success — without summaries or caveats: the form says
"Done". The original photos stay in "Raw text" for cross-checking.
"""
import base64
import hashlib
import html as html_lib
import io
import logging
import re

from bs4 import BeautifulSoup, NavigableString
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

MAX_IMAGES = 8            # embedded photos per request (each is a separate vision input)
MAX_BYTES = 15 * 1024 * 1024
MAX_PIXELS = 40_000_000   # protection against decompression "bombs"
MAX_SIDE = 1600           # longer side, px: enough for small digits and does not eat tokens
KINDS = ('photo', 'table', 'text', 'other')

_DATA_URI = re.compile(r'^data:image/(?:png|jpe?g|webp|gif);base64,', re.I)
_MEDIA_SRC = re.compile(r'^/media/[\w\-./%]+$')   # only safe characters: src ends up in an attribute
# The photo marker is [[PHOTO n]]. The legacy Ukrainian spelling [[ФОТО n]] is still accepted when parsing,
# because the model may echo it back (the marker appears in the prompt files).
_MARK = re.compile(r'\[\[\s*(?:PHOTO|ФОТО)\s*(\d+)\s*\]\]', re.I)
_MARK_ALONE = re.compile(r'<p>\s*\[\[\s*(?:PHOTO|ФОТО)\s*(\d+)\s*\]\]\s*</p>', re.I)
# Start of the fixed last line of every bundle description (the prompt tells the model to write it
# verbatim: "<p>The bundle includes a printed instruction with a detailed description of all treatments.</p>").
_LAST_LINE = '<p>The bundle includes a printed instruction'


class DescriptionImageError(Exception):
    """The photo could not be processed — the message text is shown to the manager."""


class DraftImage:
    __slots__ = ('n', 'jpeg', 'url', 'alt', 'kind', 'html', 'caption', 'text', 'note')

    def __init__(self, n, jpeg=None, url='', alt=''):
        self.n = n
        self.jpeg = jpeg      # bytes for vision and storage; None — it is already a file in media
        self.url = url        # file address, if the illustration is already in media
        self.alt = alt
        self.kind = 'photo' if url else ''
        self.html = ''        # table: the table
        self.caption = ''     # table: the heading above the table (it does not live in the <table> itself — Quill has no colspan)
        self.text = ''        # text: the transcription
        self.note = ''


class Prepared:
    def __init__(self, text, drafts, notes):
        self.text = text      # text for the model: photos replaced with a table/transcription/marker
        self.drafts = drafts
        self.notes = notes    # lines for the manager


def _mark(n):
    return f'[[PHOTO {n}]]'


def _clean_alt(s):
    s = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', str(s or ''))).strip()
    return html_lib.escape(s[:120], quote=True)


def _from_data_uri(n, src, alt):
    """data: URI → DraftImage with a JPEG ≤ MAX_SIDE. None if the image cannot be read."""
    try:
        raw = base64.b64decode(src.split(',', 1)[1])
        if not raw or len(raw) > MAX_BYTES:
            return None
        im = Image.open(io.BytesIO(raw))
        if im.width * im.height > MAX_PIXELS:
            return None
        im = ImageOps.exif_transpose(im)
        if im.mode != 'RGB':
            rgba = im.convert('RGBA')   # transparency → white background, not black
            im = Image.new('RGB', rgba.size, 'white')
            im.paste(rgba, mask=rgba.getchannel('A'))
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90, optimize=True)
        return DraftImage(n, jpeg=buf.getvalue(), alt=_clean_alt(alt))
    except Exception:
        logger.warning('Photo %s in the description draft could not be read', n, exc_info=True)
        return None


def extract_images(html):
    """HTML → (HTML with [[PHOTO n]] markers instead of <img>, [DraftImage], notes).

    Only embedded data: photos and those already stored in /media/ are processed. Images behind an
    external link are not downloaded (the server does not fetch arbitrary URLs) —
    they are removed from the text, and the manager sees a note about it."""
    if not html or '<img' not in html.lower():
        return html, [], []
    soup = BeautifulSoup(html, 'html.parser')
    drafts, embedded, external, unreadable, over = [], 0, 0, 0, 0
    for img in soup.find_all('img'):
        src = (img.get('src') or '').strip()
        n = len(drafts) + 1
        draft = None
        if _DATA_URI.match(src):
            if embedded >= MAX_IMAGES:
                over += 1
            else:
                embedded += 1
                draft = _from_data_uri(n, src, img.get('alt'))
                unreadable += draft is None
        elif _MEDIA_SRC.match(src):
            draft = DraftImage(n, url=src, alt=_clean_alt(img.get('alt')))
        elif src.startswith('data:'):    # svg/heic/broken data URI
            unreadable += 1
        else:
            external += 1
        if draft:
            drafts.append(draft)
            img.replace_with(NavigableString(_mark(n)))
        else:
            img.decompose()
    notes = []
    if external:
        notes.append(f'Images given as a link were not processed: {external} — insert them as a file.')
    if unreadable:
        notes.append(f'Could not read images: {unreadable}.')
    if over:
        notes.append(f'There are more than {MAX_IMAGES} photos in the text — the extra ones ({over}) were skipped.')
    return str(soup), drafts, notes


def clean_tables(html):
    """Brings tables to <table><tr><td>, which Quill reads without loss.

    Quill merges a thead/th header into a single cell ("MonthDose") and does not know
    colspan,
    so: thead/tbody disappear, th → td with <strong>, attributes and nested markup
    are dropped, colspan → empty cells on the right (alignment is preserved,
    values are not duplicated)."""
    if not html or '<table' not in html.lower():
        return html
    soup = BeautifulSoup(html, 'html.parser')
    for table in soup.find_all('table'):
        out = soup.new_tag('table')
        for tr in table.find_all('tr'):
            row = soup.new_tag('tr')
            for cell in tr.find_all(['td', 'th']):
                text = re.sub(r'\s+', ' ', cell.get_text(' ')).strip()
                bold = re.sub(r'\s+', ' ', ' '.join(b.get_text(' ') for b in cell.find_all(['strong', 'b']))).strip()
                td = soup.new_tag('td')
                if text and (cell.name == 'th' or bold == text):
                    strong = soup.new_tag('strong')
                    strong.string = text
                    td.append(strong)
                elif text:
                    td.append(NavigableString(text))
                row.append(td)
                try:
                    span = int(cell.get('colspan') or 1)
                except ValueError:
                    span = 1
                for _ in range(min(max(span, 1), 12) - 1):
                    row.append(soup.new_tag('td'))
            if row.contents:
                out.append(row)
        if out.contents:
            table.replace_with(out)
        else:
            table.decompose()
    return str(soup)


def _prompt():
    return (settings.BASE_DIR / 'catalog' / 'prompts' / 'description_photos.txt').read_text(encoding='utf-8')


def classify(drafts):
    """One vision call for all new photos: fills kind/alt/html/text.

    A failure raises DescriptionImageError rather than a silent "dropped the photo":
    better to ask
    for a retry than to lose the table data."""
    todo = [d for d in drafts if d.jpeg]
    if not todo:
        return
    from core import llm
    prompt = _prompt() + f'\n\nImages: {len(todo)} (numbered 1…{len(todo)} — in the order provided).'
    data = None
    for attempt in (1, 2):
        try:
            data = llm.images_to_json([(d.jpeg, 'image/jpeg') for d in todo], prompt=prompt, timeout=120)
            break
        except ValueError as e:     # broken JSON — a model slip, the second attempt usually succeeds
            logger.warning('Vision returned an unreadable response (attempt %s): %s', attempt, e)
        except Exception as e:
            raise DescriptionImageError(f'Could not parse the photos: {e}') from e
    if data is None:
        raise DescriptionImageError('Could not parse the photos: the model returned an unreadable response. Please try again.')

    answers = {}
    for it in (data.get('images') if isinstance(data, dict) else None) or []:
        try:
            answers[int(it.get('n'))] = it
        except (TypeError, ValueError, AttributeError):
            continue
    for i, d in enumerate(todo, 1):
        _apply_answer(d, answers.get(i) or {})
    logger.info('Description photos: %s', [(d.n, d.kind) for d in todo])


def _apply_answer(d, it):
    kind = it.get('kind') if it.get('kind') in KINDS else ''
    if kind == 'table':
        d.html = clean_tables(str(it.get('html') or ''))
        d.caption = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', str(it.get('caption') or ''))).strip()
        if '<table' not in d.html.lower():
            d.note = f'Could not recognize the table in photo {d.n} — the photo was kept as an illustration.'
            kind = 'photo'
    elif kind == 'text':
        d.text = str(it.get('text') or '').strip()
        if not d.text:
            d.note = f'Could not recognize the text in photo {d.n} — the photo was kept as an illustration.'
            kind = 'photo'
    elif not kind:
        d.note = f'Could not determine the type of photo {d.n} — it was kept as an illustration.'
        kind = 'photo'
    d.kind = kind
    if kind == 'photo' and not d.alt:
        d.alt = _clean_alt(it.get('alt'))


def merge_into_text(html, drafts):
    """A table/transcription becomes part of the text for the model; illustrations remain
    as markers (the model carries them into the response); the rest disappears."""
    by_n = {d.n: d for d in drafts}

    def replacement(n):
        d = by_n.get(n)
        if d is None or d.kind == 'other':
            return ''
        if d.kind == 'table':
            title = f'<p><strong>{html_lib.escape(d.caption)}</strong></p>' if d.caption else ''
            return title + d.html
        if d.kind == 'text':
            return ''.join(f'<p>{html_lib.escape(line)}</p>' for line in d.text.splitlines() if line.strip())
        return _mark(n)

    def alone(m):   # Quill puts a photo in a separate <p>: we unwrap it together with the photo so the <table> does not end up inside a <p>
        n = int(m.group(1))
        d = by_n.get(n)
        return f'<p>{replacement(n)}</p>' if d is not None and d.kind == 'photo' else replacement(n)

    html = _MARK_ALONE.sub(alone, html)
    return _MARK.sub(lambda m: replacement(int(m.group(1))), html)


def has_text(html):
    return bool(re.sub(r'<[^>]*>|&nbsp;|\s', '', html or ''))


def prepare(html):
    """The step before the model: photos → tables/text/markers. DescriptionImageError if vision failed."""
    text, drafts, notes = extract_images(html)
    if drafts:
        classify(drafts)
        text = merge_into_text(text, drafts)
        notes += [d.note for d in drafts if d.note]
    return Prepared(text, drafts, notes)


def _publish(d):
    """Saves the illustration as a file in media (name = content hash: a repeated run does not breed
duplicates)."""
    if d.url:
        return d.url
    sha = hashlib.sha1(d.jpeg).hexdigest()
    name = f'bundle_description/{sha[:2]}/{sha}.jpg'
    if not default_storage.exists(name):
        default_storage.save(name, ContentFile(d.jpeg))
    d.url = default_storage.url(name)
    return d.url


def _insert_before_last_line(html, chunk):
    """The fixed last line of the description stays last — photos go before it."""
    i = html.rfind(_LAST_LINE)
    return html + chunk if i < 0 else html[:i] + chunk + html[i:]


def finish(prepared, description, application_norms):
    """Model response → (description, application_norms, notes about failures).

    Markers → <img>; illustrations the model did not place are appended to the description (photos
    are
    not lost silently); tables — into the format Quill reads."""
    photos = {d.n: d for d in prepared.drafts if d.kind == 'photo'}
    placed = set()

    def img(d):
        return f'<img src="{html_lib.escape(_publish(d), quote=True)}" alt="{d.alt}">'

    def put(html):
        def sub(m):
            d = photos.get(int(m.group(1)))
            if d is None or d.n in placed:
                return ''
            placed.add(d.n)
            return img(d)
        return _MARK.sub(sub, html or '')

    description, application_norms = put(description), put(application_norms)
    unplaced = 0
    for d in photos.values():
        if d.n in placed:
            continue
        if description.strip():
            description = _insert_before_last_line(description, f'<p>{img(d)}</p>')
        elif application_norms.strip():
            application_norms += f'<p>{img(d)}</p>'
        else:
            unplaced += 1

    notes = list(prepared.notes)
    if unplaced:
        notes.append(f'Illustrations not placed: {unplaced} — the response has neither a description nor usage norms.')
    return clean_tables(description), clean_tables(application_norms), notes
