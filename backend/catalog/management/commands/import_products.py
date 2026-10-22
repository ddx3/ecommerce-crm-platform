"""
Universal product import — Horoshop-style xlsx as the primary source, with
optional supplementary sources to fill in what xlsx doesn't carry:

  --xlsx         Main source.  Defines which products/variants exist.
  --legacy-xlsx  Older xlsx export with extra columns (icons → labels, discount%,
                 META keywords).  Used to ENRICH variants found in --xlsx.  Rows
                 in legacy that don't appear in the main file are ignored.
  --xml          Horoshop Prom-style XML feed (URL or local path).  Provides the
                 full image gallery (up to 9 pictures per offer) and exact stock.
                 Used to ENRICH images and (optionally) stock.

Behaviour:
  default      upsert by sku, prune variants/products missing from --xlsx.
  --clear      wipe catalog (and orders/invoice mappings) before importing.
  --skip-images   don't download any photos.
  --no-prune      keep variants/products absent from --xlsx (sync-friendly).

Column lookup is done by HEADER NAME (normalized), not column index, so the
same code works across Horoshop's 35-column and 65-column exports.

Example:
    python manage.py import_products \\
        --xlsx        products_extended.xlsx \\
        --legacy-xlsx products.xlsx \\
        --xml         https://magiyaroslyn.com/content/export/FEED_TOKEN.xml \\
        --clear
"""
import contextlib
import os
import re
import time
import traceback
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation

import openpyxl
import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    Attribute, AttributeValue, Brand, Bundle, BundleItem, Category, Label,
    Product, ProductCharacteristic, ProductImage, ProductVariant,
)
from slugify import slugify


# === Header → canonical key mapping ==========================================
# Keys are matched against normalized headers (lowercase, no spaces, parens or
# punctuation).  All known variants of a column should map to the same key.
HEADER_ALIASES = {
    'sku':              ['sku'],
    'parent_sku':       ['parentsku'],
    'product_name':     ['nameua', 'name'],
    'variant_name':     ['variantnameua', 'variantname'],
    'slug':             ['slug'],
    'brand':            ['brand'],
    'category':         ['category'],
    'extra_categories': ['extracategories'],
    'price':            ['price'],
    'is_active':        ['visible'],
    'availability':     ['availability'],
    'photo':            ['photo'],
    'gallery':          ['gallery'],
    'created_at':       ['createdat'],
    'icons':            ['icons'],
    'discount':         ['discount'],
    'quantity':         ['quantity'],
    'description':      ['descriptionua', 'description'],
}

# Variant-level attributes — header alias → canonical Attribute name in DB.
# Language data: the keys are the (Ukrainian/Russian) column headers of the supplier xlsx. The
# values are the attribute names stored in the catalog DB: "Color", "Volume", "Weight" and
# "Quantity" are English (the admin UI looks them up by these names); the rest are Ukrainian names
# (Розмір нарукавників = Sleeve size, Розмір пояса = Belt size, Розмір рукавиць = Glove size,
# Ширина = Width, Довжина = Length, Кількість в пакуванні = Quantity per pack, Діаметр = Diameter,
# Висота = Height) and stay in Ukrainian.
# Two columns can map to the same attribute (e.g. Horoshop has two "Довжина" (Length)
# columns for length/blade-length); both will be merged under one Attribute.
ATTR_HEADER_TO_NAME = {
    'цвет':                    'Color',
    "обʼєм":                   'Volume',
    "обєм":                    'Volume',
    'вага':                    'Weight',
    'розмірнарукавників':      'Розмір нарукавників',
    'розмірпояса':             'Розмір пояса',
    # 'колірфурнітури' (hardware color) is deliberately not imported — we have only the single
    # "Color" attribute
    'розміррукавиць':          'Розмір рукавиць',
    'ширина':                  'Ширина',
    'довжина':                 'Довжина',
    'кількістьвпакуванні':     'Кількість в пакуванні',
    'діаметр':                 'Діаметр',
    'висота':                  'Висота',
    'кількість':               'Quantity',
}

# Attributes that should auto-flip to is_color=True when first encountered.
COLOR_ATTR_NAMES = {'Color'}

# Known colour names → HEX.  Used to pre-fill AttributeValue.color_hex so the
# UI swatch is meaningful.  Lowercase, both genders included.
# Language data: the keys are the Ukrainian colour words typed in the xlsx (чорний = black,
# білий = white, сірий = grey, зелений = green, коричневий = brown, помаранчевий = orange,
# червоний = red, жовтий = yellow, синій = blue, блакитний = light blue, бірюзовий = turquoise,
# фіолетовий = violet, рожевий = pink, бежевий = beige, молочний = milky, світлий коньяк = light
# cognac, золотий = gold, срібний = silver, бронзовий = bronze), so they stay in Ukrainian.
COLOR_HEX_MAP = {
    'чорний':'#1f1f1f','чорна':'#1f1f1f',
    'білий':'#ffffff','біла':'#ffffff',
    'сірий':'#9ca3af','сіра':'#9ca3af',
    'світло-сірий':'#d4d4d8','світло-сіра':'#d4d4d8',
    'графіт':'#2d2d2d',
    'зелений':'#22c55e','зелена':'#22c55e',
    'зелений кактус':'#5e8c5a',
    'коричневий':'#92400e','коричнева':'#92400e',
    'помаранчевий':'#f97316','помаранчева':'#f97316',
    'червоний':'#ef4444','червона':'#ef4444',
    'жовтий':'#facc15','жовта':'#facc15',
    'синій':'#3b82f6','синя':'#3b82f6',
    'темно-синій':'#1e40af',
    'блакитний':'#38bdf8','блакитна':'#38bdf8',
    'лазурний':'#6cb4ee',
    'бірюзовий':'#14b8a6',
    'фіолетовий':'#8b5cf6','фіолетова':'#8b5cf6',
    'рожевий':'#ec4899','рожева':'#ec4899',
    'бежевий':'#d2b48c','бежева':'#d2b48c',
    'молочний':'#f5e6c4',
    'світлий коньяк':'#c8956d',
    # hardware
    'золотий':'#d4af37',
    'срібний':'#c0c0c0',
    'бронзовий':'#cd7f32',
}


def color_name_to_hex(name, fallback_image_path=None):
    """
    Resolve a colour name to a 7-char HEX (#rrggbb).
    Lookup in COLOR_HEX_MAP first; if unknown, sample the centre of the given
    fallback image (assumed to be a product photo on a white background).
    Returns '' when nothing reasonable can be derived.
    """
    if not name:
        return ''
    key = str(name).strip().lower()
    if key in COLOR_HEX_MAP:
        return COLOR_HEX_MAP[key]
    if fallback_image_path:
        try:
            from PIL import Image
            with Image.open(fallback_image_path) as im:
                rgb = im.convert('RGB')
                w, h = rgb.size
                cx, cy = w // 2, h // 2
                # Sample centre 1/3 × 1/3 patch — avoids corner labels and white margins.
                patch = rgb.crop((cx - w//6, cy - h//6, cx + w//6, cy + h//6))
                # Average RGB of the patch
                small = patch.resize((1, 1), Image.LANCZOS)
                r, g, b = small.getpixel((0, 0))
                return f'#{r:02x}{g:02x}{b:02x}'
        except Exception:
            pass
    return ''

# Icon → Label slug. All labels are currently system ones (auto-logic via signals
# or recompute commands), so here we keep only the mapping for backward compat
# when importing old xlsx files. The «Хіт» (Hit) icon is currently overridden by the
# recompute_khit command, and a manual setting from the xlsx will be erased at the next
# recalculation.
# Language data: the keys are the icon names used in the Ukrainian xlsx ('Хіт' = Hit,
# 'Спеціальна пропозиція!' = Special offer!).
ICON_TO_LABEL = {
    'Хіт':                    'khit',
    'Спеціальна пропозиція!': 'aktsia',
}

ICON_SEPARATORS = re.compile(r'[;,]\s*|\n')


# === Helpers ==================================================================

def normalize_header(s):
    """'Discount %' → 'discount', 'Name (UA)' → 'nameua'."""
    if s is None:
        return ''
    return re.sub(r'[\s\(\)\-_,/.%:;!? ]+', '', str(s).strip().lower())


def split_urls(v):
    """Photo cell may carry multiple URLs separated by ';' / newline."""
    if is_blank(v):
        return []
    return [u.strip() for u in re.split(r'[;\n\r]+', str(v)) if u.strip().startswith('http')]


def is_blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def normalize_sku(v):
    if v is None:
        return ''
    s = str(v).strip()
    return s.zfill(4) if s else ''


def parse_decimal(v):
    if is_blank(v):
        return None
    try:
        d = Decimal(str(v).replace(',', '.'))
        return d if d > 0 else None
    except (InvalidOperation, ValueError):
        return None


def parse_int(v, default=0):
    if is_blank(v):
        return default
    try:
        return int(float(str(v).replace(',', '.')))
    except (ValueError, TypeError):
        return default


def parse_dt(v):
    if is_blank(v):
        return None
    dt = None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def parse_bool_yes(v):
    # Language data: 'так' is the Ukrainian "yes" used in the xlsx.
    return str(v or '').strip().lower() in ('так', 'yes', 'true', '1')


def split_icons(v):
    if is_blank(v):
        return []
    return [p.strip() for p in ICON_SEPARATORS.split(str(v)) if p.strip()]


def get_or_create_category_path(path_str, cache):
    if is_blank(path_str):
        return None
    path_str = str(path_str).strip()
    if path_str in cache:
        return cache[path_str]
    parent = None
    built = ''
    for part in (p.strip() for p in path_str.split('/') if p.strip()):
        built = f'{built}/{part}' if built else part
        if built in cache:
            parent = cache[built]
            continue
        obj, _ = Category.objects.get_or_create(
            name=part, parent=parent, defaults={'is_active': True}
        )
        cache[built] = obj
        parent = obj
    cache[path_str] = parent
    return parent


def get_or_create_brand(name, cache):
    if is_blank(name):
        return None
    key = str(name).strip()
    if key in cache:
        return cache[key]
    obj, _ = Brand.objects.get_or_create(name=key)
    cache[key] = obj
    return obj


def get_or_create_attr_value(attr_name, value, attr_cache, value_cache,
                             fallback_image_path=None):
    """
    Get-or-create an AttributeValue.  For colour attributes (in COLOR_ATTR_NAMES)
    we additionally:
      - flip Attribute.is_color=True the first time we see the attribute;
      - resolve color_hex from the value name (with optional photo fallback).
    """
    if is_blank(value):
        return None
    val = str(value).strip()
    attr = attr_cache.get(attr_name)
    if attr is None:
        attr, _ = Attribute.objects.get_or_create(name=attr_name)
        attr_cache[attr_name] = attr
    if attr_name in COLOR_ATTR_NAMES and not attr.is_color:
        attr.is_color = True
        attr.save(update_fields=['is_color'])
    key = (attr.pk, val)
    if key in value_cache:
        return value_cache[key]
    av, _ = AttributeValue.objects.get_or_create(attribute=attr, value=val)
    if attr.is_color and not av.color_hex:
        h = color_name_to_hex(val, fallback_image_path)
        if h:
            av.color_hex = h
            av.save(update_fields=['color_hex'])
    value_cache[key] = av
    return av


def download_image(url, retries=2, timeout=15):
    if is_blank(url) or not str(url).startswith('http'):
        return None
    raw = os.path.basename(str(url).split('?')[0]) or 'image.jpg'
    base, ext = os.path.splitext(raw)
    fname = base[:80] + ext[:10]
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code == 200 and r.content:
                return fname, ContentFile(r.content)
        except Exception:
            if attempt < retries:
                time.sleep(1)
    return None


def unique_slug(model, base, exclude_pk=None):
    base = base or 'item'
    slug = base
    n = 1
    qs = model.objects.filter(slug=slug)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    while qs.exists():
        n += 1
        slug = f'{base}-{n}'
        qs = model.objects.filter(slug=slug)
        if exclude_pk:
            qs = qs.exclude(pk=exclude_pk)
    return slug


def build_header_index(ws):
    """
    Read row 1 and return:
      keys   : { canonical_key: column_index }   for HEADER_ALIASES
      attrs  : { column_index: Attribute name }  for ATTR_HEADER_TO_NAME
    Duplicate attribute headers (e.g. two "Довжина" (Length)) are disambiguated by
    appending " 2", " 3" to the second/third occurrence.
    """
    keys = {}
    attrs = {}
    alias_to_key = {}
    for k, aliases in HEADER_ALIASES.items():
        for a in aliases:
            alias_to_key[a] = k
    seen_attr_names = {}  # base name → count
    for c in range(1, ws.max_column + 1):
        h = normalize_header(ws.cell(1, c).value)
        if not h:
            continue
        if h in alias_to_key:
            keys.setdefault(alias_to_key[h], c)
        if h in ATTR_HEADER_TO_NAME:
            base = ATTR_HEADER_TO_NAME[h]
            n = seen_attr_names.get(base, 0) + 1
            seen_attr_names[base] = n
            attrs[c] = base if n == 1 else f'{base} {n}'
    return keys, attrs


# === XML feed parser ==========================================================

def fetch_xml(src):
    """src is URL or local path."""
    if src.startswith('http'):
        return urllib.request.urlopen(src, timeout=60).read().decode('utf-8', errors='replace')
    with open(src, encoding='utf-8') as f:
        return f.read()


_OFFER_RE = re.compile(r'<offer\s+id="\d+"[^>]*>(.*?)</offer>', re.S)
_SKU_RE   = re.compile(r'<vendorCode>\s*(.*?)\s*</vendorCode>')
_PIC_RE   = re.compile(r'<picture>\s*(.*?)\s*</picture>')
_QTY_RE   = re.compile(r'<quantity_in_stock>\s*(\d+)\s*</quantity_in_stock>')


def parse_xml_feed(text):
    """Return { sku: {'pics': [url, ...], 'qty': int|None} }."""
    out = {}
    for m in _OFFER_RE.finditer(text):
        body = m.group(1)
        sm = _SKU_RE.search(body)
        if not sm:
            continue
        sku = normalize_sku(sm.group(1))
        out[sku] = {
            'pics': _PIC_RE.findall(body),
            'qty':  int(_QTY_RE.search(body).group(1)) if _QTY_RE.search(body) else None,
        }
    return out


# === Legacy xlsx parser =======================================================

def parse_legacy_xlsx(path):
    """
    Return { sku: {'icons': [...], 'discount': Decimal|None, 'created_at': dt|None,
                   'photo': str, 'gallery': str} }.
    Used to back-fill what the new xlsx doesn't carry — labels (icons),
    discount %, original Horoshop creation timestamps, and photo URLs that
    were dropped from the main export.
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    keys, _ = build_header_index(ws)

    out = {}
    for r in range(2, ws.max_row + 1):
        sku = normalize_sku(ws.cell(r, keys.get('sku', 0)).value if keys.get('sku') else None)
        if not sku:
            continue
        out[sku] = {
            'icons':      split_icons(ws.cell(r, keys['icons']).value) if 'icons' in keys else [],
            'discount':   parse_decimal(ws.cell(r, keys['discount']).value) if 'discount' in keys else None,
            'created_at': parse_dt(ws.cell(r, keys['created_at']).value) if 'created_at' in keys else None,
            'photo':      str(ws.cell(r, keys['photo']).value or '') if 'photo' in keys else '',
            'gallery':    str(ws.cell(r, keys['gallery']).value or '') if 'gallery' in keys else '',
        }
    return out


# === Command ==================================================================

class Command(BaseCommand):
    help = 'Universal hybrid product import (xlsx + legacy xlsx + Prom XML feed).'

    def add_arguments(self, parser):
        parser.add_argument('--xlsx', required=True, help='Main xlsx (Horoshop export)')
        parser.add_argument('--legacy-xlsx', help='Older xlsx with icons/discount/keywords')
        parser.add_argument('--xml', help='Prom XML feed URL or path (full gallery + qty)')
        parser.add_argument('--clear', action='store_true', help='Wipe catalog before import')
        parser.add_argument('--skip-images', action='store_true')
        parser.add_argument('--no-prune', action='store_true',
                            help='Keep variants absent from main xlsx (sync-friendly)')
        parser.add_argument('--use-xml-stock', action='store_true',
                            help='Override stock with quantity_in_stock from XML feed')
        parser.add_argument('--dry-run', action='store_true',
                            help='Write nothing: show for every product '
                                 'which fields would change / what would be created / what is extra.')

    def handle(self, *args, **opts):
        if not os.path.exists(opts['xlsx']):
            raise CommandError(f"--xlsx not found: {opts['xlsx']}")
        if opts.get('legacy_xlsx') and not os.path.exists(opts['legacy_xlsx']):
            raise CommandError(f"--legacy-xlsx not found: {opts['legacy_xlsx']}")

        self.dry_run = opts.get('dry_run', False)
        if self.dry_run:
            # In dry-run we write to the DB inside a transaction and roll it back at the end,
            # and we don't touch images at all (files are written outside the transaction).
            opts['skip_images'] = True
            self.stdout.write(self.style.WARNING(
                '=== DRY-RUN: nothing will be saved, only a report of the changes ===\n'))

        if opts['clear']:
            if self.dry_run:
                raise CommandError('--dry-run is incompatible with --clear.')
            self._wipe()

        # --- Load supplementary sources ---
        legacy = {}
        if opts.get('legacy_xlsx'):
            self.stdout.write(f"Loading legacy xlsx: {opts['legacy_xlsx']}")
            legacy = parse_legacy_xlsx(opts['legacy_xlsx'])
            self.stdout.write(f'  {len(legacy)} legacy SKU records')

        xml_data = {}
        if opts.get('xml'):
            self.stdout.write(f"Loading XML feed: {opts['xml']}")
            try:
                xml_text = fetch_xml(opts['xml'])
                xml_data = parse_xml_feed(xml_text)
                self.stdout.write(f'  {len(xml_data)} offers from XML')
            except Exception as e:
                self.stderr.write(f'  XML feed failed: {e}')

        # --- Read main xlsx ---
        self.stdout.write(f"Reading {opts['xlsx']}...")
        wb = openpyxl.load_workbook(opts['xlsx'], data_only=True)
        ws = wb.active
        keys, attr_cols = build_header_index(ws)

        required = {'sku', 'parent_sku', 'price'}
        missing = required - set(keys)
        if missing:
            raise CommandError(f'Required columns missing in main xlsx: {missing}')

        rows = []
        for r in range(2, ws.max_row + 1):
            row = tuple(ws.cell(r, c).value for c in range(1, ws.max_column + 1))
            if row[keys['sku'] - 1] is None:
                continue
            rows.append(row)

        # --- Business filters ----------------------------------------------------
        # 1) Skip rows whose category starts with "Комплекти" (Bundles) — bundles have a
        #    separate import flow. (Language data: the category names come from the Ukrainian xlsx.)
        # 2) Skip rows where variant name starts with "*" AND brand is "Bradas" —
        #    these are dropshipping products handled by sync_dropshipping.
        # Variant names starting with "*" with any other brand (or no brand) are
        # treated as hybrid 50/50 and get is_hybrid=True later in _upsert_variant.
        cat_idx = keys.get('category')
        name_idx = keys.get('variant_name') or keys.get('product_name')
        brand_idx = keys.get('brand')
        skipped_bundle = 0
        skipped_drop = 0
        filtered = []
        for row in rows:
            category = str(row[cat_idx - 1] or '') if cat_idx else ''
            name = str(row[name_idx - 1] or '') if name_idx else ''
            brand = str(row[brand_idx - 1] or '').strip() if brand_idx else ''
            if category.startswith('Комплекти'):
                skipped_bundle += 1
                continue
            if name.startswith('*') and brand == 'Bradas':
                skipped_drop += 1
                continue
            filtered.append(row)
        self.stdout.write(
            f'Filtered out: {skipped_bundle} bundle rows, {skipped_drop} dropship rows (Bradas with *)'
        )
        rows = filtered

        # Group by parent_sku
        groups = {}
        for row in rows:
            sku = normalize_sku(row[keys['sku'] - 1])
            parent = normalize_sku(row[keys['parent_sku'] - 1]) or sku
            groups.setdefault(parent, []).append((sku, row))

        self.stdout.write(f'Loaded {len(rows)} rows / {len(groups)} products')

        # --- Caches ---
        cat_cache   = {}
        brand_cache = {b.name: b for b in Brand.objects.all()}
        attr_cache  = {a.name: a for a in Attribute.objects.all()}
        value_cache = {}
        label_cache = self._build_label_cache()

        seen_variant_skus = set()
        seen_product_pks  = set()

        stats = dict(products_created=0, products_updated=0,
                     variants_created=0, variants_updated=0,
                     images_downloaded=0, images_failed=0,
                     legacy_applied=0, errors=0)

        # In dry-run everything runs in one transaction that is rolled back at the end —
        # the DB stays unchanged, but the code goes through the real path and we see
        # the exact diffs. In a real run there is NO outer transaction: each
        # group is committed separately (as before), to avoid holding one long
        # transaction while images are downloading.
        outer = transaction.atomic() if self.dry_run else contextlib.nullcontext()
        outer.__enter__()
        try:
            total = len(groups)
            for i, (parent_sku, group_rows) in enumerate(groups.items(), 1):
                try:
                    self._import_group(
                        parent_sku, group_rows, keys, attr_cols,
                        cat_cache, brand_cache, attr_cache, value_cache, label_cache,
                        legacy, xml_data,
                        seen_variant_skus, seen_product_pks,
                        stats, opts['skip_images'], opts['use_xml_stock'],
                    )
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(f'ERROR product {parent_sku}: {e}')
                    traceback.print_exc()

                if i % 25 == 0 or i == total:
                    self.stdout.write(f'  {i}/{total} processed')

            if not opts['clear']:
                if opts['no_prune'] or self.dry_run:
                    # We delete nothing — we only report what is in the DB but not
                    # in the file (excluding dropshipping/bundles/own packaging).
                    self._report_missing(seen_variant_skus)
                else:
                    self._prune(seen_variant_skus, seen_product_pks)
        finally:
            if self.dry_run:
                transaction.set_rollback(True)
            outer.__exit__(None, None, None)

        if self.dry_run:
            self.stdout.write(self.style.WARNING(
                '\n=== DRY-RUN finished: changes ROLLED BACK, DB not modified ==='))
        self._report(stats)

    # --- catalog wipe --------------------------------------------------------

    def _wipe(self):
        self.stdout.write('Wiping catalog (and dependent orders/invoice mappings)...')
        from orders.models import Order, InvoiceNameMapping
        Order.objects.all().delete()
        InvoiceNameMapping.objects.all().delete()

        ProductImage.objects.all().delete()
        ProductCharacteristic.objects.all().delete()
        BundleItem.objects.all().delete()
        Bundle.objects.all().delete()
        ProductVariant.objects.all().delete()
        Product.objects.all().delete()
        Brand.objects.all().delete()
        Category.objects.all().delete()

    def _build_label_cache(self):
        # All labels are currently system ones — return the existing ones without creating custom
        # ones.
        return {l.slug: l for l in Label.objects.all() if l.slug}

    # --- per-product import --------------------------------------------------

    def _import_group(self, parent_sku, group_rows, keys, attr_cols,
                      cat_cache, brand_cache, attr_cache, value_cache, label_cache,
                      legacy, xml_data,
                      seen_variant_skus, seen_product_pks,
                      stats, skip_images, use_xml_stock):
        head_row = next((r for s, r in group_rows if s == parent_sku), group_rows[0][1])

        with transaction.atomic():
            product = self._upsert_product(head_row, parent_sku, keys, cat_cache,
                                           brand_cache, label_cache, legacy, stats)
            seen_product_pks.add(product.pk)

            variant_map = {}  # sku → ProductVariant
            for variant_sku, row in group_rows:
                v = self._upsert_variant(
                    product, variant_sku, row, keys, attr_cols,
                    attr_cache, value_cache, legacy, xml_data,
                    stats, use_xml_stock,
                )
                variant_map[variant_sku] = v
                seen_variant_skus.add(variant_sku)

            # «Наша фасовка» (own packaging) (packaging_kind='own'): variants are different
            # packagings
            # of ONE product that share a gallery (variant=None). Horoshop's logic
            # would scatter photos across variants and break the album,
            # so we do NOT sync photos for such products at all.
            if not skip_images and product.packaging_kind != Product.PACKAGING_OWN:
                self._import_images_for_group(product, group_rows, keys,
                                              variant_map, xml_data, legacy, stats)
            elif product.packaging_kind == Product.PACKAGING_OWN:
                stats['images_skipped_own'] = stats.get('images_skipped_own', 0) + 1

    def _upsert_product(self, row, parent_sku, keys, cat_cache, brand_cache,
                        label_cache, legacy, stats):
        def col(k):
            i = keys.get(k)
            return row[i - 1] if i else None

        name = (col('product_name') or col('variant_name') or f'Product {parent_sku}')
        name = str(name).strip()

        brand = get_or_create_brand(col('brand'), brand_cache)
        description = str(col('description') or '').strip()
        is_active  = parse_bool_yes(col('is_active'))

        cats = []
        # ';' separates alternative categories (Horoshop), '/' is the hierarchy
        primary = col('category')
        if primary:
            for branch in str(primary).split(';'):
                c = get_or_create_category_path(branch, cat_cache)
                if c and c not in cats:
                    cats.append(c)
        if (extras := col('extra_categories')):
            for path in str(extras).split(';'):
                c = get_or_create_category_path(path, cat_cache)
                if c and c not in cats:
                    cats.append(c)

        # Find existing product via head variant
        product = None
        existing = ProductVariant.objects.filter(sku=parent_sku).select_related('product').first()
        if existing:
            product = existing.product

        defaults = {
            'name': name,
            'description': description,
            'is_active': is_active,
            'brand': brand,
        }
        if product is None:
            self._diff_log(f'+ NEW PRODUCT [{parent_sku}] {name}', [])
            product = Product(slug=unique_slug(Product, slugify(name) or f'product-{parent_sku}'),
                              **defaults)
            product.save()
            stats['products_created'] += 1
        else:
            changes = self._field_changes(product, defaults)
            if changes:
                self._diff_log(f'~ PRODUCT [{parent_sku}] {product.name}', changes)
            for k, v in defaults.items():
                setattr(product, k, v)
            product.save()
            stats['products_updated'] += 1

        if cats:
            product.categories.set(cats)

        # --- Labels: from current xlsx (icons col) + legacy fallback ---
        icons = []
        if 'icons' in keys:
            icons = split_icons(col('icons'))
        leg = legacy.get(parent_sku) if legacy else None
        if not icons and leg and leg.get('icons'):
            icons = leg['icons']
            stats['legacy_applied'] += 1

        slugs = []
        for icon in icons:
            slug = ICON_TO_LABEL.get(icon, ...)
            if slug is ...:
                self.stderr.write(f'  Unknown icon "{icon}" (product {parent_sku})')
                continue
            if slug:
                slugs.append(slug)
        product.labels.set([label_cache[s] for s in slugs if s in label_cache])

        return product

    def _upsert_variant(self, product, variant_sku, row, keys, attr_cols,
                        attr_cache, value_cache, legacy, xml_data,
                        stats, use_xml_stock):
        def col(k):
            i = keys.get(k)
            return row[i - 1] if i else None

        variant_name = str(col('variant_name') or product.name).strip()

        # Hybrid 50/50: any remaining variant starting with "*" is hybrid
        # (Bradas-with-* dropship rows were filtered out earlier in handle()).
        is_hybrid = variant_name.startswith('*')
        if is_hybrid:
            variant_name = variant_name.lstrip('*').strip()

        price = parse_decimal(col('price')) or Decimal('0')

        # Stock: xml override > xlsx availability+qty
        if use_xml_stock and variant_sku in xml_data and xml_data[variant_sku].get('qty') is not None:
            stock = max(0, xml_data[variant_sku]['qty'])
        else:
            availability = str(col('availability') or '').strip()
            qty = parse_int(col('quantity'), 0)
            # Language data: 'Немає в наявності' = "Out of stock", the availability value in the xlsx.
            stock = 0 if availability == 'Немає в наявності' else max(0, qty)

        is_active = parse_bool_yes(col('is_active'))

        # Discount: from current xlsx, fallback to legacy
        discount = parse_decimal(col('discount'))
        if discount is None and legacy.get(variant_sku):
            discount = legacy[variant_sku].get('discount')

        # Variant slug — from the Alias column if present, else slugify(name) with the
        # SKU appended for uniqueness.
        slug_raw = str(col('slug') or '').strip() or slugify(variant_name)
        variant_slug = unique_slug(ProductVariant, slug_raw or f'variant-{variant_sku}',
                                   exclude_pk=ProductVariant.objects.filter(sku=variant_sku)
                                                                    .values_list('pk', flat=True).first())

        defaults = {
            'product': product,
            'name': variant_name,
            'slug': variant_slug,
            'price': price,
            'discount_percent': discount,
            'stock': stock,
            'is_active': is_active,
            'is_hybrid': is_hybrid,
        }

        # «Наша фасовка» (own packaging): for existing variants with their own packaging (a bulk bag
        # or
        # a pack size), stock is computed automatically from BulkSource — we do NOT
        # overwrite it with the value from the xlsx. Other fields (price/name/description) are fine.
        existing = ProductVariant.objects.filter(sku=variant_sku).first()
        if existing and (existing.unit_size or existing.bulk_source_id):
            defaults.pop('stock', None)
            stats['packaging_stock_preserved'] = stats.get('packaging_stock_preserved', 0) + 1

        if existing is None:
            self._diff_log(f'  + new variant [{variant_sku}] {variant_name}', [])
        else:
            changes = self._field_changes(existing, defaults)
            if changes:
                self._diff_log(f'  ~ variant [{variant_sku}] {existing.name}', changes)

        variant, created = ProductVariant.objects.update_or_create(
            sku=variant_sku,
            defaults=defaults,
        )
        stats['variants_created' if created else 'variants_updated'] += 1

        # created_at: prefer current xlsx, else legacy
        dt = parse_dt(col('created_at'))
        if dt is None and legacy.get(variant_sku):
            dt = legacy[variant_sku].get('created_at')
        if dt:
            ProductVariant.objects.filter(pk=variant.pk).update(created_at=dt)

        # Attributes (M2M).  We do NOT mirror them into ProductCharacteristic
        # — the frontend renders attribute_values as the spec table (single
        # source of truth, no rows duplicated across two models).
        variant.characteristics.all().delete()
        attr_values = []
        for col_idx, attr_name in attr_cols.items():
            raw = row[col_idx - 1]
            if is_blank(raw):
                continue
            av = get_or_create_attr_value(attr_name, raw, attr_cache, value_cache)
            if av:
                attr_values.append(av)

        # MERGE, not replace: attributes the file carries are updated; attributes that are
        # NOT in the file are left as they are. Otherwise we would overwrite values that
        # distinguish variants (e.g. «Колір фурнітури» (Hardware color) on belt bags, which is
        # deliberately not imported from the xlsx) — and the variants would become
        # indistinguishable.
        file_attr_ids = {av.attribute_id for av in attr_values}
        kept = [av for av in variant.attribute_values.all()
                if av.attribute_id not in file_attr_ids]
        variant.attribute_values.set(kept + attr_values)
        if kept:
            stats['attrs_preserved'] = stats.get('attrs_preserved', 0) + len(kept)
        return variant

    # --- images: dedup at the product level ---------------------------------

    def _collect_urls_for_variant(self, row, keys, variant_sku, xml_data, legacy=None):
        """All distinct image URLs declared for a single variant row, ordered.

        Sources, in order: main xlsx Photo → main xlsx Gallery → XML feed pics
        → legacy xlsx Photo/Gallery (only if main xlsx didn't supply anything).
        """
        out = []
        seen = set()
        def add(u):
            if u and u not in seen:
                out.append(u); seen.add(u)

        i = keys.get('photo')
        if i:
            for u in split_urls(row[i - 1]): add(u)
        i = keys.get('gallery')
        if i:
            for u in split_urls(row[i - 1]): add(u)
        for u in (xml_data.get(variant_sku, {}).get('pics') or []):
            add(u.strip())

        # Legacy fallback: only if main xlsx + XML didn't yield any URLs for
        # this variant.  Useful for products whose photo links got dropped
        # from the newer Horoshop export.
        if not out and legacy:
            leg = legacy.get(variant_sku) or {}
            for u in split_urls(leg.get('photo')):    add(u)
            for u in split_urls(leg.get('gallery')):  add(u)
        return out

    def _import_images_for_group(self, product, group_rows, keys, variant_map,
                                 xml_data, legacy, stats):
        """
        Idempotent image sync — supports re-running on updated xlsx.

        For each Product (with its group of variants):
          1) Collect ALL URLs declared in the current xlsx for this group.
          2) Group URLs into shared (product-level) vs per-variant — same logic
             as before (a URL referenced by ≥2 variants, or in a single-variant
             product, or when only one variant supplied photos = shared).
          3) Compare against existing ProductImage rows that have a non-empty
             source_url:
                - already in DB AND still in xlsx → keep, just refresh
                  variant/sort_order/is_main if changed.
                - in DB but no longer in xlsx → delete (Horoshop removed it).
                - in xlsx but not in DB → download and save.
          4) Images with source_url = NULL are user-uploaded — never touched.
        """
        # --- Collect URLs ---
        url_to_skus = {}  # url → set(sku)
        url_order   = {}  # url → first index seen (for sort ordering)
        global_idx  = 0
        for variant_sku, row in group_rows:
            for url in self._collect_urls_for_variant(row, keys, variant_sku, xml_data, legacy):
                url_to_skus.setdefault(url, set()).add(variant_sku)
                url_order.setdefault(url, global_idx)
                global_idx += 1

        single_variant_product = (len(variant_map) == 1)

        # Horoshop export pattern: often only the parent row has photos, other
        # variant rows have empty Photo/Gallery cells.  In that case the photo
        # is meant as the product's main photo, shared across variants — not
        # an exclusive photo of the parent variant.
        variants_supplying_photos = set()
        for skus in url_to_skus.values():
            variants_supplying_photos.update(skus)
        only_one_variant_supplied = (len(variants_supplying_photos) == 1)

        # Group URLs: shared (→ product) and exclusive (→ variant)
        shared_urls = []          # list[str]
        per_variant = {}          # sku → list[str]
        for url, skus in url_to_skus.items():
            if len(skus) >= 2 or single_variant_product or only_one_variant_supplied:
                shared_urls.append(url)
            else:
                sku = next(iter(skus))
                per_variant.setdefault(sku, []).append(url)

        shared_urls.sort(key=lambda u: url_order[u])
        for lst in per_variant.values():
            lst.sort(key=lambda u: url_order[u])

        # url → desired (variant_obj_or_None, sort_order)
        desired = {}
        for idx, url in enumerate(shared_urls):
            desired[url] = (None, idx)
        for sku, urls in per_variant.items():
            v = variant_map.get(sku)
            if not v:
                continue
            for idx, url in enumerate(urls):
                desired[url] = (v, idx)

        # --- Existing imported images for this product (have source_url) ---
        existing = {
            img.source_url: img
            for img in product.images.filter(source_url__isnull=False)
                                     .exclude(source_url='')
        }

        # 1) Delete images that were imported but no longer in xlsx
        stale_urls = set(existing) - set(desired)
        for url in stale_urls:
            existing[url].delete()
            stats['images_removed'] = stats.get('images_removed', 0) + 1
        for url in stale_urls:
            existing.pop(url, None)

        # 2) Update existing or download new
        for url, (variant, sort_order) in desired.items():
            is_main = (sort_order == 0)
            if url in existing:
                img = existing[url]
                changed_fields = []
                if img.variant_id != (variant.pk if variant else None):
                    img.variant = variant; changed_fields.append('variant')
                if img.sort_order != sort_order:
                    img.sort_order = sort_order; changed_fields.append('sort_order')
                if img.is_main != is_main:
                    img.is_main = is_main; changed_fields.append('is_main')
                if changed_fields:
                    img.save(update_fields=changed_fields)
                    stats['images_updated'] = stats.get('images_updated', 0) + 1
            else:
                self._save_image(product, variant, url, sort_order, stats)

        # 3) Post-process: in a multi-variant product, if some variants ended
        # up with no images at all (e.g. their Gallery URLs returned 404), and
        # there are no shared images either — promote the surviving variant's
        # images to shared (variant=None) so every variant has *something* to
        # display on the storefront.
        if len(variant_map) > 1:
            variants_with_imgs = [
                v for v in variant_map.values() if v.images.exists()
            ]
            variants_without_imgs = [
                v for v in variant_map.values() if not v.images.exists()
            ]
            has_shared = product.images.filter(variant__isnull=True).exists()
            if variants_without_imgs and not has_shared and len(variants_with_imgs) == 1:
                # Exactly one variant has images, others have none — these are
                # really the product's main photos, not variant-specific.
                only_var = variants_with_imgs[0]
                n = ProductImage.objects.filter(variant=only_var).update(variant=None)
                stats['images_promoted_shared'] = (
                    stats.get('images_promoted_shared', 0) + n
                )

    def _save_image(self, product, variant, url, sort_order, stats):
        result = download_image(url)
        if not result:
            stats['images_failed'] += 1
            return
        fname, content = result
        img = ProductImage(
            product=product,
            variant=variant,
            sort_order=sort_order,
            is_main=(sort_order == 0),
            source_url=url,
        )
        img.image.save(fname, content, save=True)
        stats['images_downloaded'] += 1

    # --- pruning -------------------------------------------------------------

    # Variants that we NEVER delete by pruning and never show in the report on
    # "extra" ones — they live in the DB independently of this xlsx:
    #   - dropshipping (sync_dropshipping is the source of truth);
    #   - own packaging (bulk bag / pack size / paired packaging are created in the admin).
    def _protected_variant_filter(self):
        from django.db.models import Q
        return (
            Q(is_dropshipping=True)
            | Q(unit_size__isnull=False)
            | Q(bulk_source__isnull=False)
            | Q(paired_variant__isnull=False)
            | Q(product__packaging_kind=Product.PACKAGING_OWN)
        )

    # --- diff helpers (used by --dry-run and normal run for visibility) ------

    def _field_changes(self, obj, defaults):
        """Returns [(field, old, new), ...] for the fields that will actually change.
        FKs are compared by pk; long text is truncated when displayed."""
        changes = []
        for field, new in defaults.items():
            old = getattr(obj, field, None)
            # FK fields are passed in defaults as objects — compare by pk
            if hasattr(old, 'pk') or hasattr(new, 'pk'):
                old_pk = getattr(old, 'pk', None)
                new_pk = getattr(new, 'pk', None)
                if old_pk != new_pk:
                    changes.append((field, getattr(old, 'name', old), getattr(new, 'name', new)))
                continue
            # Normalize the comparison of numbers/Decimal and strings
            if str(old) != str(new) and old != new:
                changes.append((field, old, new))
        return changes

    def _trunc(self, v, n=60):
        s = str(v).replace('\n', ' ').strip()
        return s if len(s) <= n else s[:n] + '…'

    def _diff_log(self, header, changes):
        self.stdout.write(header)
        for field, old, new in changes:
            self.stdout.write(f'      {field}: {self._trunc(old)} → {self._trunc(new)}')

    def _prune(self, seen_variant_skus, seen_product_pks):
        qs = (ProductVariant.objects
              .exclude(sku__in=seen_variant_skus)
              .exclude(self._protected_variant_filter()))
        deleted_v = qs.count()
        qs.delete()
        # Only delete products that have NO variants left at all (including dropship).
        empty = (Product.objects.filter(variants__isnull=True)
                 .exclude(packaging_kind=Product.PACKAGING_OWN))
        deleted_p = empty.count()
        empty.delete()
        if deleted_v or deleted_p:
            self.stdout.write(f'Pruned: {deleted_v} variants, {deleted_p} empty products')

    def _report_missing(self, seen_variant_skus):
        """Shows products/variants that are in the DB but absent from the new file.
        Excludes protected types (dropshipping, packaging). Deletes nothing —
        the decision is up to a human."""
        qs = (ProductVariant.objects
              .exclude(sku__in=seen_variant_skus)
              .exclude(self._protected_variant_filter())
              .select_related('product')
              .order_by('product__name', 'name'))
        rows = list(qs)
        if not rows:
            self.stdout.write(self.style.SUCCESS(
                '\nAll products from the DB (except dropshipping/own packaging) are present in the file.'))
            return
        self.stdout.write(self.style.WARNING(
            f'\n⚠ IN THE DB BUT NOT IN THE FILE — {len(rows)} variant(s) (nothing was deleted):'))
        for v in rows:
            self.stdout.write(
                f'  [{v.sku}] {v.product.name} — {v.name} '
                f'(active={v.is_active}, stock={v.stock})')

    def _report(self, stats):
        self.stdout.write(self.style.SUCCESS('Import complete:'))
        for k, v in stats.items():
            self.stdout.write(f'  {k}: {v}')
