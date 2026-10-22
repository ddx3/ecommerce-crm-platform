"""
Universal dropshipping import — parses bradas.ua category pages, formats
variant names by category type, groups variants under one Product, and
loads everything into the catalog.

Behaviour:
  - 4 categories supported (configured in CATEGORY_CONFIGS):
        agrotkани           → "Агротканина, шпильки/Агротканина" (Agrotextile, pins/Agrotextile)
        setki               → "Сітка для затінення" (Shade netting)
        bordyury-sadovye    → "Бордюри садові" (Garden borders)
        aksesuary (шпильки) → "Агротканина, шпильки/Шпильки" (Agrotextile, pins/Pins)
  - Brand "Bradas" assigned to every product.
  - Idempotent by `supplier_sku` (re-run upserts existing rows).
  - Photo dedup at the product level (shared photos go on product, exclusive
    on variant).
  - is_active = stock > 0 for variants; product is active if any variant is.
  - Price = round_down_5(supplier_price), rounds down to nearest 5 UAH.

Usage:
    python manage.py import_dropshipping
    python manage.py import_dropshipping --only agrotkани setki
    python manage.py import_dropshipping --skip-images
    python manage.py import_dropshipping --threads 25
"""
import hashlib
import math
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from slugify import slugify

from catalog.models import (
    Attribute, Brand,
    Product, ProductCharacteristic, ProductImage, ProductVariant,
)
from catalog.management.commands.import_products import (
    download_image, get_or_create_attr_value, get_or_create_brand,
    get_or_create_category_path, unique_slug,
)


HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

BRAND_NAME = 'Bradas'

# LANGUAGE DATA (whole module): the supplier pages (bradas.ua) are in Ukrainian/Russian and the shop
# catalog (category paths, product names, most attribute names, labels) is stored in Ukrainian. The
# Ukrainian words below are therefore scraped-spec keys, regexes that match what the supplier
# writes (г/м² = g/m², мм/см/м = mm/cm/m, шт = pcs, Колір = Color, Ширина = Width, Довжина = Length,
# Висота = Height, Щільність = Density, Кількість = Quantity, Упаковка = Packaging, Матеріал =
# Material) and names written into / looked up in the catalog DB, so they stay in Ukrainian.
# Exception: the destination attributes "Color" and "Quantity" (v_attrs / v_chars below) use their
# English names, which the admin UI looks up; the supplier's own spec keys stay Ukrainian.
# Each cluster has a short English gloss next to it.

# === Helpers ==================================================================

def round_down_5(price):
    """Round the price down to 5 UAH.

    Below 5 UAH we round down to a whole hryvnia rather than to a multiple of five — otherwise
    everything that costs less than 5 (pins, stakes) would get a price of 0 and be sold for free."""
    p = float(price)
    if p < 5:
        return math.floor(p)
    return math.floor(p / 5) * 5


def strip_corner_label(raw_bytes, fname='img.jpg', red_threshold=500):
    """
    Bradas burns a red 'NEW' ribbon into some pictures.  Position varies —
    seen in top-left, top-right (and probably others).  Sample all 4 corners,
    paint over any that contain enough red.  Background of bradas catalog
    photos is white, so a white rectangle blends invisibly.
    """
    try:
        with Image.open(BytesIO(raw_bytes)) as im:
            rgb = im.convert('RGB')
            w, h = rgb.size
            box_w = min(int(w * 0.30), 250)
            box_h = min(int(h * 0.18), 150)

            corners = [
                ('TL', (0, 0, box_w, box_h)),
                ('TR', (w - box_w, 0, w, box_h)),
                ('BL', (0, h - box_h, box_w, h)),
                ('BR', (w - box_w, h - box_h, w, h)),
            ]
            modified = False
            out = rgb.copy()
            draw = ImageDraw.Draw(out)
            for _, box in corners:
                crop = rgb.crop(box).getdata()
                red = sum(1 for r, g, b in crop if r > 150 and r > g + 50 and r > b + 50)
                if red >= red_threshold:
                    draw.rectangle(box, fill=(255, 255, 255))
                    modified = True
            if not modified:
                return raw_bytes, False

            buf = BytesIO()
            ext = (fname.rsplit('.', 1)[-1] or 'jpg').lower()
            fmt = 'PNG' if ext == 'png' else 'JPEG'
            save_kwargs = {'quality': 95} if fmt == 'JPEG' else {}
            out.save(buf, format=fmt, **save_kwargs)
            return buf.getvalue(), True
    except Exception:
        return raw_bytes, False


def perceptual_hash(raw_bytes):
    """
    Combined hash:  dHash (shape) on grayscale 9×8  +  average RGB 4×4.
    Returns "<16-hex dhash>|<6-hex avg-rgb>" — 23 chars, or None.
    """
    try:
        with Image.open(BytesIO(raw_bytes)) as im:
            # 1) Shape — dHash on grayscale
            gray = im.convert('L').resize((9, 8))
            gpx = list(gray.getdata())
            bits = 0
            for row in range(8):
                for col in range(8):
                    bits = (bits << 1) | (1 if gpx[row * 9 + col] > gpx[row * 9 + col + 1] else 0)

            # 2) Colour — average RGB of 4×4 downscale
            rgb = im.convert('RGB').resize((4, 4))
            spx = list(rgb.getdata())
            n = len(spx)
            avg_r = sum(p[0] for p in spx) // n
            avg_g = sum(p[1] for p in spx) // n
            avg_b = sum(p[2] for p in spx) // n

            return f'{bits:016x}|{avg_r:02x}{avg_g:02x}{avg_b:02x}'
    except Exception:
        return None


def near_duplicate(h1, h2, hamming_thresh=5, color_thresh=30):
    """
    Two perceptual hashes are 'near duplicates' iff:
      - dHash differs by ≤ hamming_thresh bits (out of 64)  AND
      - average RGB differs by ≤ color_thresh (sum of |Δr|+|Δg|+|Δb|, max 765)
    """
    if '|' not in h1 or '|' not in h2:
        return False
    b1, c1 = h1.split('|')
    b2, c2 = h2.split('|')
    if bin(int(b1, 16) ^ int(b2, 16)).count('1') > hamming_thresh:
        return False
    r1, g1, bl1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
    r2, g2, bl2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
    return abs(r1 - r2) + abs(g1 - g2) + abs(bl1 - bl2) <= color_thresh


def _generate_sku():
    """Random 4-digit SKU not used by any variant or bundle."""
    return ProductVariant.generate_unique_sku(also_check_bundle=True)


def _dim_to_m(val, as_length=False):
    """'160 см' (cm) → '1.6', '5м' (m) → '5', '1.6' → '1.6'.

    as_length=True is for roll length: there 50/100 are meters, not centimeters,
    so we do NOT apply the «>10 means cm» heuristic (otherwise 100м → 1м)."""
    nums = re.findall(r'\d+[.,]?\d*', str(val))
    if not nums:
        return ''
    n = float(nums[0].replace(',', '.'))
    if not as_length and n > 10:
        n /= 100
    return f'{n:g}'


def _dims_from_sku_agrotkan(sku):
    """The Bradas SKU suffix encodes the size: 2 digits of width (tenths) + 3 of length.
    AT7032005 → 3.2х5м, ATGDBK10021050 → 2.1х50м. The most reliable source:
    the supplier's specs are often empty, and URLs contain typos."""
    m = re.search(r'(\d{2})(\d{3})$', (sku or '').upper())
    if not m:
        return '', ''
    w = int(m.group(1)) / 10
    l = int(m.group(2))
    if not (0 < w <= 10 and 0 < l <= 300):
        return '', ''
    return f'{w:g}', f'{l:g}'


# === Bradas page parsing ======================================================

def parse_listing(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'html.parser')
    lst = soup.find_all('div', class_='bxr-list')
    if not lst:
        return []
    return lst[0].find_all('div', class_='bxr-ecommerce-v3-lite')


def get_product_details(url, retries=2):
    """Fetch a bradas detail page; retry once if photos / props came back empty
    (this typically means we got rate-limited or a partial page)."""
    last_result = ('', {}, 0, [])
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.encoding = 'utf-8'
            soup = BeautifulSoup(resp.text, 'html.parser')

            desc_div = soup.find('div', class_='bxr-detail-text')
            description = desc_div.get_text(separator=' ', strip=True) if desc_div else ''

            props = {}
            props_div = soup.find('div', class_='bxr-detail-props')
            if props_div:
                for tr in props_div.find_all('tr'):
                    n = tr.find('td', class_='bxr-props-name')
                    v = tr.find('td', class_='bxr-props-data')
                    if n and v:
                        props[n.get_text(strip=True)] = v.get_text(strip=True)

            qty_input = soup.find('input', id='quantity_text')
            stock = int(qty_input['data-quantity']) if qty_input and qty_input.has_attr('data-quantity') else 0

            # Minimum batch. For some products bradas shows the price per 1 piece,
            # but sells only in packs (data-min=50) — then the real purchase cost
            # is price × data-min, otherwise a pack of 50 pins would cost the same as one.
            min_qty = 1
            minus_btn = soup.find('input', class_='bxr-quantity-button-minus')
            if minus_btn and minus_btn.has_attr('data-min'):
                try:
                    min_qty = max(1, int(minus_btn['data-min']))
                except (TypeError, ValueError):
                    min_qty = 1
            props['_min_qty'] = min_qty

            photos = []
            slider = soup.find('div', class_='ax-element-slider-main')
            if slider:
                for a in slider.find_all('a', class_='fancybox'):
                    href = a.get('href', '')
                    if href:
                        photos.append(href if href.startswith('http') else 'https://bradas.ua' + href)
            if not photos:
                img = soup.find('img', class_='bxr-detail-picture')
                if img:
                    src = img.get('src') or img.get('data-src', '')
                    if src:
                        photos.append(src if src.startswith('http') else 'https://bradas.ua' + src)

            last_result = (description, props, stock, photos)
            # Page looks valid → done.  If photos and props are both empty we
            # probably got a stub page — retry.
            if photos or props:
                return last_result
        except Exception:
            pass
        if attempt < retries:
            time.sleep(1 + attempt)  # mild backoff
    return last_result


def process_item(item):
    try:
        name_tag = item.find('div', class_='bxr-element-name').find('a')
        name = name_tag.get_text(strip=True)
        link = 'https://bradas.ua' + name_tag.get('href')

        price_tag = item.find('span', class_='bxr-market-current-price')
        price_value = 0.0
        if price_tag:
            cleaned = re.sub(r'[^\d.,]', '', price_tag.get_text(' ', strip=True))
            cleaned = cleaned.replace(',', '.').strip('.')
            if cleaned:
                try:
                    price_value = float(cleaned)
                except ValueError:
                    pass

        desc, specs, stock, photos = get_product_details(link)
        supplier_sku = (specs.get('Артикул', '') or (name.split()[-1] if name else '')).strip()

        # The price on the page may be per 1 piece with a mandatory batch — we convert
        # it to the cost of what actually has to be bought.
        min_qty = int(specs.get('_min_qty', 1) or 1)
        if min_qty > 1:
            price_value = price_value * min_qty

        return {
            'name':           name,
            'supplier_sku':   supplier_sku,
            'supplier_min_qty': min_qty,
            'supplier_price': price_value,
            'stock':          stock,
            'url':            link,
            'description':    desc,
            'specs':          specs,
            'photos':         photos,
        }
    except Exception:
        return None


# === Group / format functions per category ===================================
# Each function returns: { group_key: [(row, v_attrs_dict, v_chars_list), ...] }
# - row['name'] is REPLACED with a formatted variant name
# - v_attrs   — variant attribute values for ProductVariant.attribute_values
# - v_chars   — list of (name, value) pairs for ProductCharacteristic


def normalize_variant_attrs_to_characteristics(product, stats=None):
    """
    Turns «shared» variant attributes into product characteristics.

    Rule: an attribute exists to DISTINGUISH variants. If all active
    variants of a product have the same value for an attribute, it is not an attribute
    but a characteristic of the whole product.

    Algorithm:
      1) Build a map attribute_id → set(value).
      2) For attributes with a single value:
         - create/update ProductCharacteristic(product=…, variant=None);
         - remove this AttributeValue from every variant.attribute_values;
         - remove the duplicate characteristic at variant level (same name).
    Note: works only with variants currently attached to the product.
    If a product has one variant, all of its attributes become characteristics.
    """
    from catalog.models import ProductCharacteristic
    variants = list(product.variants.all().prefetch_related('attribute_values__attribute'))
    if not variants:
        return

    # attribute_id → {AttributeValue.pk: AttributeValue}
    by_attr = {}
    attr_objs = {}
    for v in variants:
        seen_in_v = set()
        for av in v.attribute_values.all():
            seen_in_v.add(av.attribute_id)
            attr_objs[av.attribute_id] = av.attribute
            by_attr.setdefault(av.attribute_id, {}).setdefault(av.pk, av)

    moved = 0
    for attr_id, av_map in by_attr.items():
        # Do all variants have the same value for this attribute?
        # «One shared AttributeValue» means:
        #   - all variants reference it (not different AVs of the same attr);
        #   - there are no other AVs for this attr.
        if len(av_map) != 1:
            continue
        av = next(iter(av_map.values()))
        # Check that all variants have it
        all_have = all(
            any(x.pk == av.pk for x in v.attribute_values.all())
            for v in variants
        )
        if not all_have:
            continue

        attr = attr_objs[attr_id]
        # Create/update the product characteristic
        ProductCharacteristic.objects.update_or_create(
            product=product, variant=None, name=attr.name,
            defaults={'value': av.value, 'sort_order': 0},
        )
        # Remove it from the variants
        for v in variants:
            v.attribute_values.remove(av)
            # Duplicate characteristic at variant level — remove it
            v.characteristics.filter(name__iexact=attr.name).delete()
        moved += 1

    if stats is not None and moved:
        stats['attrs_moved_to_characteristics'] = (
            stats.get('attrs_moved_to_characteristics', 0) + moved
        )


def _join_nonempty(parts, sep=' '):
    """Joins only non-empty parts, ignoring None/''/whitespace.
    Protects the formatters from stray separators (×, comma) when
    the supplier did not return some field (height/length/color)."""
    return sep.join(str(p).strip() for p in parts if p and str(p).strip())


def _compose_name(base, *segments):
    """
    Builds a name from optional segments. Each segment is either a string
    or a (prefix, value) tuple — if value is empty, the segment is skipped.
    Example: _compose_name('EASY BORDER', '40 мм × 10 м', (', ', 'Коричневий'))
             → 'EASY BORDER 40 мм × 10 м, Коричневий'
    """
    out = str(base).strip()
    for seg in segments:
        if isinstance(seg, tuple):
            prefix, value = seg
            if value and str(value).strip():
                out += f'{prefix}{str(value).strip()}'
        elif seg and str(seg).strip():
            if not out.endswith((' ', ',', '(')):
                out += ' '
            out += str(seg).strip()
    return out

def group_agrotkань(rows):
    groups = defaultdict(list)
    # Language data: Ukrainian colour stems -> normalised catalog colour
    # (чорна = black, зелена = green, помаранчева = orange).
    color_norm = {
        'чорн': 'Чорна', 'black': 'Чорна',
        'зелен': 'Зелена', 'green': 'Зелена',
        'помаранч': 'Помаранчева', 'orange': 'Помаранчева',
    }
    for row in rows:
        name = row['name']
        specs = row['specs']

        color_raw = str(specs.get('Колір', '') or specs.get('КолірВиберіть колір', '')).strip()
        color = ''
        for key, val in color_norm.items():
            if key in color_raw.lower():
                color = val; break
        if not color:
            for key, val in color_norm.items():
                if key in name.lower():
                    color = val; break
        color = color or 'Чорна'

        # Density — only the number next to «г/м²» (g/m²). You cannot just take the first number:
        # in the «GRID 25» line (25 cm mesh) the first number is the grid pitch, not the density.
        density_raw = str(specs.get('Щільність', ''))
        dm = (re.search(r'(\d+)\s*(?:г|гр|g|gr)\s*/?\s*м', density_raw or name, re.IGNORECASE)
              or re.search(r'(\d+)', density_raw))
        density = dm.group(1) if dm else ''

        # Size: the SKU suffix is the most reliable; specs/name is the fallback.
        w, l = _dims_from_sku_agrotkan(row.get('supplier_sku', ''))
        if not (w and l):
            w_raw = str(specs.get('Ширина', ''))
            l_raw = str(specs.get('Довжина', ''))
            if not w_raw or w_raw in ('nan', 'None', '—'):
                m = re.search(r'([\d.,]+)\s?[хxХX×]\s?([\d.,]+)', name)
                if m: w_raw, l_raw = m.group(1), m.group(2)
            w, l = _dim_to_m(w_raw), _dim_to_m(l_raw, as_length=True)

        density_str = f'{density} г/м²' if density else ''
        group_key = _compose_name('Агротканина', density_str, (' (', color))
        if color and group_key.endswith(color):
            group_key += ')'

        v_attrs = {}
        if color: v_attrs['Color'] = color
        if w: v_attrs['Ширина'] = w
        if l: v_attrs['Довжина'] = l

        v_chars = [
            ('Color', color),
            ('Щільність', density_str),
            ('Ширина', f'{w} м' if w else ''),
            ('Довжина', f'{l} м' if l else ''),
        ]

        # Dimensions: "WхLм" if both are present and look realistic.
        def _ok(x):
            try:
                f = float(str(x).replace(',', '.'))
                return 0 < f <= 300
            except (TypeError, ValueError):
                return False

        dims = f'{w}х{l}м' if (w and l and _ok(w) and _ok(l)) else ''
        formatted = _compose_name('Агротканина', density_str)
        if dims:
            formatted = _compose_name(formatted, (', ', dims))
        if color:
            formatted += f' ({color})'
        row = dict(row); row['name'] = formatted
        groups[group_key].append((row, v_attrs, v_chars))
    return groups


def _is_agrovolokno_fabric(sku):
    """Agrofibre sheet: black (AWB*) and white (AWW + digit).
    Hoods, sleeves and other accessories (AWWC*, AWWR*) are not sheet material."""
    sku = (sku or '').upper()
    if sku.startswith('AWB'):
        return True
    m = re.match(r'AWW(.)', sku)
    return bool(m and m.group(1).isdigit())


def group_agrovolokno(rows):
    """Agrofibre: we import the sheet material — black (AWB*) and white (AWW*).
    Hoods, sleeves and other accessories are deliberately skipped."""
    groups = defaultdict(list)
    for row in rows:
        sku = row['supplier_sku'].upper()
        if not _is_agrovolokno_fabric(sku):
            continue
        name = row['name']
        specs = row['specs']

        # Language data: Чорне = Black, Біле = White (catalog colour values).
        color = 'Чорне' if sku.startswith('AWB') else 'Біле'

        density_raw = str(specs.get('Щільність', ''))
        dm = re.search(r'(\d+)', density_raw or name)
        density_str = f'{dm.group(1)} г/м²' if dm else ''

        # We take the size from the name — it always has a reliable format «1,6х10м».
        m = re.search(r'([\d.,]+)\s?[хxХX×]\s?([\d.,]+)\s?м', name)
        w = m.group(1).replace(',', '.') if m else ''
        l = m.group(2).replace(',', '.') if m else ''

        group_key = _compose_name('Агроволокно', density_str) + f' ({color})'

        v_attrs = {'Color': color}
        if w: v_attrs['Ширина'] = w
        if l: v_attrs['Довжина'] = l

        v_chars = [
            ('Щільність', density_str),
            ('Ширина', f'{w} м' if w else ''),
            ('Довжина', f'{l} м' if l else ''),
        ]

        dims = f'{w}х{l}м' if (w and l) else ''
        formatted = _compose_name('Агроволокно', density_str)
        if dims:
            formatted += f', {dims}'
        formatted += f' ({color})'

        row = dict(row); row['name'] = formatted
        groups[group_key].append((row, v_attrs, v_chars))
    return groups


# The first block of digits in the AS-CO SKU is the DENSITY in g/m² (the supplier calls
# the product «Сітка для затінення, захисна, 95%, 160 г/м²» (Shade netting, protective, 95%, 160
# g/m²)), not the mesh cell size.
# We try candidates from larger to smaller, otherwise «38…» would match «3» and the remaining
# digits would go into the width (which is where the 8.1 m rolls came from).
_SETKA_DENSITIES = [160, 135, 130, 120, 110, 105, 90, 60, 38, 30, 20, 15, 13, 10, 7, 5, 4, 3, 2]


def _sku_dims_setka(sku):
    """-> (density, width_m, length_m) from the AS-CO<density><WW><LLL> SKU."""
    sku = sku.upper()
    m = re.search(r'AS-CO(\d+)', sku)
    if not m:
        return None, None, None
    digits = m.group(1)
    for size in _SETKA_DENSITIES:
        s = str(size)
        if digits.startswith(s):
            rest = digits[len(s):]
            if len(rest) >= 5:
                w_tenths = int(rest[:2])
                length = int(rest[2:])
                # Sanity check: width ≤10 m, length ≤300 m.
                # Otherwise the SKU code clearly does not match the current pattern —
                # return None and let the fallback from name sort it out.
                if 0 < w_tenths <= 100 and 0 < length <= 300:
                    w = w_tenths / 10
                    w_str = str(int(w)) if w == int(w) else f'{w:.1f}'
                    return f'{size}', w_str, str(length)
            break
    return None, None, None


def _color_setka(sku, name):
    # Language data: matches Ukrainian colour stems in the supplier name and returns the catalog colour
    # (зелена = green, чорна = black, сіра = grey, біла = white).
    sku = sku.upper()
    low = name.lower()
    if sku.endswith('GR') or sku.endswith('GR/P') or 'зелен' in low: return 'Зелена'
    if any(x in sku for x in ('BK', 'BW')) or 'чорн' in low:        return 'Чорна'
    if sku.endswith('GY') or sku.endswith('GY/P') or 'сір' in low:  return 'Сіра'
    if sku.endswith('SB') or 'біл' in low:                          return 'Біла'
    return ''


def group_setki(rows):
    groups = defaultdict(list)
    for row in rows:
        sku = row['supplier_sku'].upper()
        if not sku.startswith('AS-CO') and not re.match(r'AS.?CO', sku):
            _fallback_group(row, groups)
            continue
        # White nets (SB) are not needed — skip them.
        if sku.endswith('SB'):
            continue
        name = row['name']

        density, w, l = _sku_dims_setka(sku)
        if not density:
            dm = re.search(r'([\d.,]+)\s?[хxХX×]\s?([\d.,]+)\s?м\b', name)
            w = dm.group(1) if dm else ''
            l = dm.group(2) if dm else ''
            ds = re.search(r'(\d+)\s?г\s*/\s*м', name)
            density = ds.group(1) if ds else ''

        color = _color_setka(sku, name)
        # Previously «{N}×{N} мм» (mm) was written here, passing density off as mesh cell size —
        # this produced «сітка 160×160 мм» (net 160×160 mm), i.e. a 16 cm cell.
        dens_str = f'{density} г/м²' if density else ''
        group_key = _compose_name('Сітка захисна', dens_str)
        if color:
            group_key += f' ({color})'

        # Sanity check of dimensions — do not let unrealistic numbers into the name.
        def _dim_ok(w_, l_):
            try:
                wf = float(str(w_).replace(',', '.'))
                lf = float(str(l_).replace(',', '.'))
            except (TypeError, ValueError):
                return False
            return 0 < wf <= 100 and 0 < lf <= 300

        dims_str = f'{w}×{l}м' if (w and l and _dim_ok(w, l)) else ''
        formatted = _compose_name('Сітка захисна', dens_str)
        if dims_str:
            formatted += f', {dims_str}'
        if color:
            formatted += f' ({color})'

        packaging = _extract_packaging(row.get('specs', {}), sku)
        v_attrs = {}
        if color: v_attrs['Color'] = color
        if w: v_attrs['Ширина'] = w
        if l: v_attrs['Довжина'] = l
        if packaging: v_attrs['Упаковка'] = packaging

        # Add packaging suffix to formatted name when distinguishing duplicates.
        if packaging:
            formatted = formatted + f' ({packaging.lower()})'

        v_chars = [
            ('Щільність', dens_str),
            ('Ширина', f'{w} м' if w else ''),
            ('Довжина', f'{l} м' if l else ''),
        ]

        row = dict(row); row['name'] = formatted
        groups[group_key].append((row, v_attrs, v_chars))
    return groups


BORDYUR_PREFIXES = ('OBRB', 'OBE', 'OBKT')


def _color_bord(name):
    # Language data: matches Ukrainian colour stems in the supplier name and returns the catalog colour
    # (світло-сірий = light grey, графіт = graphite, коричневий = brown, зелений = green, чорний = black).
    low = name.lower()
    if 'світло-сір' in low or 'світло сір' in low: return 'Світло-сірий'
    if 'графіт' in low:    return 'Графіт'
    if 'коричнев' in low:  return 'Коричневий'
    if 'зелен' in low:     return 'Зелений'
    if 'чорн' in low:      return 'Чорний'
    return ''


def _height_bord(name):
    # Language data: 'мм' = mm in the supplier name.
    m = re.search(r'(\d+)[хxХX×]\d+\s?мм', name)
    if m: return f'{m.group(1)} мм'
    m = re.search(r'(\d+)\s?мм', name)
    if m: return f'{m.group(1)} мм'
    return ''


def _length_bord(name, specs, desc='', url=''):
    """Extracts the border length from several sources in this order:
       1) specs['Довжина'] (Length) (how it used to be)
       2) description: «Довжина: 1 м / 100 см / 1000 мм» (Length: 1 m / 100 cm / 1000 mm)
       3) name: «… × 10 м» or «… 100 см»
       4) URL slug: «58kh1000mm» / «58x10m»
    Returns a string with units («1 м», «10 м», ...) or '' if nothing was found.
    """
    def _norm(num, unit):
        try:
            n = float(str(num).replace(',', '.'))
        except (TypeError, ValueError):
            return ''
        unit = unit.lower()
        if unit.startswith('мм'):
            return f'{int(n // 1000)} м' if n >= 1000 else f'{n:g} мм'
        if unit.startswith('см'):
            return f'{int(n // 100)} м' if n >= 100 else f'{n:g} см'
        # 'м' (m) — already in meters
        return f'{n:g} м'

    # 1) specs
    l_raw = str(specs.get('Довжина', '')).strip()
    if l_raw and l_raw not in ('nan', 'None', '—'):
        m = re.match(r'^([\d.,]+)\s*(мм|см|м)\b', l_raw, re.IGNORECASE)
        if m:
            r = _norm(m.group(1), m.group(2))
            if r:
                return r
        return l_raw

    # 2) description: «Довжина: 1 м» (Length: 1 m)
    if desc:
        m = re.search(r'довжина\s*[:\-]?\s*([\d.,]+)\s*(мм|см|м)\b', desc, re.IGNORECASE)
        if m:
            r = _norm(m.group(1), m.group(2))
            if r:
                return r

    # 3) name: «{height}мм × {length} м» or «{n} см»
    m = re.search(r'мм\s*[хxХX×]\s*([\d.,]+)\s*м\b', name, re.IGNORECASE)
    if m:
        return f'{m.group(1)} м'
    m = re.search(r'(\d+)\s*см\b', name)
    if m:
        return _norm(m.group(1), 'см')

    # 4) URL slug: «58kh1000mm» / «58x10m» / «58kh1m»
    if url:
        m = re.search(r'\d+\s*[xх×kк]+h?\s*(\d+(?:[.,]\d+)?)\s*(мм|mm|см|cm|м|m)\b', url, re.IGNORECASE)
        if m:
            unit_norm = {'mm': 'мм', 'cm': 'см', 'm': 'м'}.get(m.group(2).lower(), m.group(2).lower())
            r = _norm(m.group(1), unit_norm)
            if r:
                return r

    return ''


def group_bordyur(rows):
    groups = defaultdict(list)
    for row in rows:
        sku = row['supplier_sku'].upper()
        if not sku.startswith(BORDYUR_PREFIXES):
            _fallback_group(row, groups)
            continue
        name = row['name']
        specs = row['specs']

        color = _color_bord(name)
        height = _height_bord(name)
        length = _length_bord(name, specs, row.get('description', ''), row.get('url', ''))

        if sku.startswith('OBKT'):
            qty_m = re.search(r'/(\d+)', sku)
            qty = qty_m.group(1) if qty_m else ''
            size_m = re.search(r'OBKT(\d+)', sku)
            size = f'{size_m.group(1)} см' if size_m else ''
            # Single Product «Кілок для бордюру» (Border stake) with variants by length and qty.
            group_key = 'Кілок для бордюру'
            formatted = _compose_name('Кілок для бордюру', size)
            if qty:
                formatted += f', {qty} шт.'
            v_attrs = {}
            if size: v_attrs['Довжина'] = size
            if qty: v_attrs['Quantity'] = qty
            v_chars = [('Quantity', f'{qty} шт.' if qty else '')]

        elif sku.startswith('OBRB'):
            if 'TYPL' in sku:
                kind = 'RIM-BORD LINEO'
            elif 'TYP2' in sku:
                kind = 'ECO RIM-BORD TYP 2'
            else:
                kind = 'RIM-BORD'
            # Single Product per series (RIM-BORD / LINEO / TYP2) with variants
            # by height and length.
            group_key = f'Бордюр садовий {kind}'
            size = _join_nonempty([height, length], sep=' × ')
            formatted = _compose_name(f'Бордюр садовий {kind}', size)
            v_attrs = {}
            if height: v_attrs['Висота'] = height
            if length: v_attrs['Довжина'] = length
            v_chars = []

        elif sku.startswith('OBE'):  # EASY BORDER
            # Single Product «Бордюр газонний EASY BORDER» (EASY BORDER lawn border) with variants
            # by height, length and colour.
            group_key = 'Бордюр газонний EASY BORDER'
            size = _join_nonempty([height, length], sep=' × ')
            formatted = _compose_name('Бордюр газонний EASY BORDER', size)
            if color:
                formatted += f', {color}'
            v_attrs = {}
            if color:  v_attrs['Color']   = color
            if height: v_attrs['Висота']  = height
            if length: v_attrs['Довжина'] = length
            v_chars = []

        else:
            _fallback_group(row, groups)
            continue

        row = dict(row); row['name'] = formatted
        groups[group_key].append((row, v_attrs, v_chars))
    return groups


SHPYLKY_PREFIXES = ('ATSMGU', 'ATS12', 'ATS15', 'ATS19', 'ATSU')


def _extract_packaging(specs, sku=''):
    """
    Resolve a packaging label from bradas specs ('Вид упаковкиВид упаковки товара' /
    'Вид упаковки' / 'Упаковка' / 'Тип упаковки', all meaning packaging type), normalised to a
    Ukrainian word ('Коробка' = Box, 'Пакет' = Bag).
    Falls back to SKU suffix heuristics when specs don't carry it.
    """
    for k in ('Вид упаковкиВид упаковки товара', 'Вид упаковки', 'Упаковка', 'Тип упаковки'):
        v = (specs or {}).get(k)
        if v:
            v = str(v).strip()
            if v and v.lower() not in ('—', 'nan', 'none'):
                return v
    s = (sku or '').upper()
    if s.endswith('BOX'):                       return 'Коробка'
    if s.endswith('/P') or s.endswith('/PAK'):  return 'Пакет'
    if s.endswith('/L') or s.endswith('100L'):  return 'Пакет'
    return ''


def group_shpylky(rows):
    groups = defaultdict(list)
    for row in rows:
        sku = row['supplier_sku'].upper()
        if not sku.startswith(SHPYLKY_PREFIXES):
            _fallback_group(row, groups)
            continue
        name = row['name']
        specs = row['specs']

        if sku.startswith('ATSMGU'):
            m = re.match(r'ATSMGU(1[37])(\d{2})/(\d+)', sku)
            if not m:
                continue
            width_mm = int(m.group(1)[1])  # 13→3, 17→7
            length_cm = int(m.group(2))
            qty = int(m.group(3))
            material = str(specs.get('Матеріал', '') or '').strip() or 'Сталь оцинкована'
            packaging = _extract_packaging(specs, sku)
            group_key = f'Шпилька U металева {width_mm}мм/{length_cm}см'
            pkg_suffix = f' ({packaging.lower()})' if packaging else ''
            formatted = f'Шпилька U металева {width_mm}мм/{length_cm}см — {qty} шт.{pkg_suffix}'
            v_attrs = {'Quantity': str(qty)}
            if packaging: v_attrs['Упаковка'] = packaging
            v_chars = [('Матеріал', material)]

        elif re.match(r'ATS(12|15|19)/', sku):
            m = re.match(r'ATS(12|15|19)/(\d+)', sku)
            if not m:
                continue
            length_cm = int(m.group(1))
            qty = int(m.group(2))
            material = str(specs.get('Матеріал', '') or '').strip() or 'Сталь'
            # Different lengths = different Products; quantity is the variant.
            group_key = f'Шпилька пряма {length_cm}см'
            formatted = f'Шпилька пряма {length_cm}см — {qty} шт.'
            v_attrs = {'Quantity': str(qty)}
            v_chars = [('Матеріал', material)]

        elif sku.startswith('ATSU'):
            m = re.match(r'ATSU(\d+)\+', sku)
            if not m:
                continue
            length_cm = int(m.group(1))
            qty_m = re.search(r'-\s*(\d+)\s*шт', name)
            qty = int(qty_m.group(1)) if qty_m else 0
            material = str(specs.get('Матеріал', '') or '').strip() or 'Пластик'
            group_key = f'Шпилька для агроволокна {length_cm}см'
            formatted = (f'Шпилька для агроволокна {length_cm}см — {qty} шт.' if qty
                         else f'Шпилька для агроволокна {length_cm}см')
            v_attrs = {'Quantity': str(qty)} if qty else {}
            v_chars = [('Матеріал', material)]
        else:
            _fallback_group(row, groups)
            continue

        row = dict(row); row['name'] = formatted
        groups[group_key].append((row, v_attrs, v_chars))
    return groups


def _fallback_group(row, groups):
    """
    Item didn't match any known SKU pattern → import as a standalone Product
    (one product per supplier_sku), name as-is from bradas, specs become
    characteristics, no attribute_values.  Marked as fallback so the visibility
    logic can hide such products until a proper formatter is written.
    """
    name = row.get('name', '') or row.get('supplier_sku', 'Product')
    specs = row.get('specs') or {}
    v_chars = []
    for k, v in specs.items():
        # keys starting with '_' are internal (e.g. _min_qty) and do not go into product
        # characteristics
        if k.startswith('_'):
            continue
        if v and str(v).strip() not in ('', '—', 'nan', 'None'):
            v_chars.append((k, str(v).strip()))
    # Use supplier_sku as the unique group key — keeps each unmatched item separate.
    group_key = name
    row = dict(row)
    row['_is_fallback'] = True
    groups[group_key].append((row, {}, v_chars))


# === Category configuration ===================================================

# Language data: 'category_path' / 'label' are the Ukrainian category path and label names stored in
# the catalog DB (Агротканина, шпильки = Agrotextile, pins; Агротканина = Agrotextile; Шпильки = Pins;
# Агроволокно = Agrofibre; Сітка для затінення = Shade netting; Сітки = Nets; Бордюри садові =
# Garden borders; Бордюри = Borders).
CATEGORY_CONFIGS = {
    'agrotkани': {
        'url':           'https://bradas.ua/catalog/agrotkani/?SHOWALL_2=1',
        'group_fn':      group_agrotkань,
        'category_path': 'Агротканина, шпильки/Агротканина',
        'label':         'Агротканина',
    },
    'agrovolokno': {
        'url':           'https://bradas.ua/catalog/agrovolokno/?SHOWALL_2=1',
        'group_fn':      group_agrovolokno,
        'category_path': 'Агроволокно',
        'label':         'Агроволокно',
    },
    'setki': {
        'url':           'https://bradas.ua/catalog/setki/?SHOWALL_2=1',
        'group_fn':      group_setki,
        'category_path': 'Сітка для затінення',
        'label':         'Сітки',
    },
    'bordyury': {
        'url':           'https://bradas.ua/catalog/bordyury-sadovye/?SHOWALL_2=1',
        'group_fn':      group_bordyur,
        'category_path': 'Бордюри садові',
        'label':         'Бордюри',
    },
    'shpylky': {
        'url':           'https://bradas.ua/catalog/shpylky/?SHOWALL_2=1',
        'group_fn':      group_shpylky,
        'category_path': 'Агротканина, шпильки/Шпильки',
        'label':         'Шпильки',
    },
}


# === Command ==================================================================

class Command(BaseCommand):
    help = 'Parse and import dropshipping products from bradas.ua.'

    def add_arguments(self, parser):
        parser.add_argument('--only', nargs='+', choices=list(CATEGORY_CONFIGS),
                            help='Import only the specified categories')
        parser.add_argument('--skip-images', action='store_true')
        parser.add_argument('--threads', type=int, default=15,
                            help='Number of HTTP threads when parsing details')
        parser.add_argument('--skip-fallback', action='store_true',
                            help='Do not save products that did not match the SKU format '
                                 '(skip them instead of creating them inactive).')

    def handle(self, *args, **opts):
        wanted = opts['only'] or list(CATEGORY_CONFIGS)

        # Caches reused across all categories
        cat_cache   = {}
        brand_cache = {b.name: b for b in Brand.objects.all()}
        attr_cache  = {a.name: a for a in Attribute.objects.all()}
        value_cache = {}
        brand = get_or_create_brand(BRAND_NAME, brand_cache)

        stats = dict(rows_parsed=0, products_created=0, products_updated=0,
                     variants_created=0, variants_updated=0,
                     images_downloaded=0, images_failed=0,
                     skipped_fallback=0, errors=0)
        skip_fallback = opts['skip_fallback']

        for key in wanted:
            cfg = CATEGORY_CONFIGS[key]
            self.stdout.write(self.style.NOTICE(f'\n=== {cfg["label"]} ({key}) ==='))

            category = get_or_create_category_path(cfg['category_path'], cat_cache)
            if category is None:
                self.stderr.write(f'  Category path resolved to None: {cfg["category_path"]}')
                continue

            try:
                items = parse_listing(cfg['url'])
            except Exception as e:
                self.stderr.write(f'  Listing fetch failed: {e}')
                stats['errors'] += 1
                continue

            self.stdout.write(f'  Listing: {len(items)} items, fetching details...')
            with ThreadPoolExecutor(max_workers=opts['threads']) as ex:
                results = list(ex.map(process_item, items))
            rows = [r for r in results if r]

            # Dedup by supplier_sku — last wins
            seen = {}
            for r in rows:
                if r['supplier_sku']:
                    seen[r['supplier_sku']] = r
            rows = list(seen.values())
            stats['rows_parsed'] += len(rows)
            self.stdout.write(f'  Unique SKUs: {len(rows)}')

            groups = cfg['group_fn'](rows)
            self.stdout.write(f'  Groups: {len(groups)}')

            for group_key, group_rows in groups.items():
                # Fallback products (those that failed the SKU format) are not imported at all —
                # they only clutter the catalog. The --skip-fallback flag is kept for backward
                # compatibility but no longer affects behaviour.
                if all(r[0].get('_is_fallback') for r in group_rows):
                    stats['skipped_fallback'] += len(group_rows)
                    continue
                try:
                    self._save_group(
                        group_key, group_rows, category, brand,
                        attr_cache, value_cache,
                        opts['skip_images'], stats,
                        skip_fallback=skip_fallback,
                    )
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(f'  ERROR group "{group_key}": {e}')

        # Second pass — products that ended up without any photos.  Almost
        # always this means the bradas detail page was rate-limited during
        # the initial parallel fetch.  Retry sequentially.
        if not opts['skip_images']:
            self._refetch_missing_photos(stats)

        self.stdout.write(self.style.SUCCESS('\nImport complete:'))
        for k, v in stats.items():
            self.stdout.write(f'  {k}: {v}')

    # --- per-group import ----------------------------------------------------

    def _save_group(self, group_key, group_rows, category, brand,
                    attr_cache, value_cache, skip_images, stats,
                    skip_fallback=False):
        head_row = group_rows[0][0]
        product, p_created = self._upsert_product(group_key, head_row, category, brand)
        stats['products_created' if p_created else 'products_updated'] += 1

        variant_map = {}  # supplier_sku → variant
        has_processed_variant = False
        for row, v_attrs, v_chars in group_rows:
            # Individual fallback rows within a group are also skipped — the catalog stays clean.
            if row.get('_is_fallback'):
                stats['skipped_fallback'] += 1
                continue
            v, v_created = self._upsert_variant(
                product, row, v_attrs, v_chars, attr_cache, value_cache,
            )
            stats['variants_created' if v_created else 'variants_updated'] += 1
            variant_map[row['supplier_sku']] = v
            if not v.is_supplier_fallback:
                has_processed_variant = True

        # Product is visible iff at least one variant came through a real
        # group_fn formatter (i.e. is NOT a fallback row).
        if product.is_active != has_processed_variant:
            product.is_active = has_processed_variant
            product.save(update_fields=['is_active'])

        if not skip_images:
            self._import_images(product, group_rows, variant_map, stats)

        # Normalize attributes: whatever is identical across all variants is moved
        # into product characteristics (an attribute exists to DISTINGUISH variants).
        normalize_variant_attrs_to_characteristics(product, stats)

    def _upsert_product(self, group_key, head_row, category, brand):
        with transaction.atomic():
            product = Product.objects.filter(name=group_key, brand=brand).first()
            if product:
                product.description = head_row['description']
                product.save()
                created = False
            else:
                product = Product(
                    name=group_key,
                    slug=unique_slug(Product, slugify(group_key) or 'dropshipping'),
                    description=head_row['description'],
                    brand=brand,
                    is_active=True,
                )
                product.save()
                created = True
            product.categories.add(category)
        return product, created

    def _upsert_variant(self, product, row, v_attrs, v_chars,
                        attr_cache, value_cache):
        supplier_sku = row['supplier_sku']
        formatted = row['name']
        supplier_price = row['supplier_price']
        our_price = round_down_5(supplier_price) if supplier_price else 0
        stock = int(row['stock'] or 0)
        now = timezone.now()

        existing = ProductVariant.objects.filter(supplier_sku=supplier_sku).first()
        is_fallback = bool(row.get('_is_fallback'))

        # For EXISTING variants we update only price/availability/link
        # and do not touch name/description/attributes/characteristics/photos.
        # Name/description/attributes are set once at first import; repeated runs
        # must never overwrite manual edits made in the admin.
        sync_only_fields = {
            'price':                Decimal(str(our_price)),
            'stock':                stock,
            'supplier_price':       Decimal(str(supplier_price)) if supplier_price else None,
            'supplier_min_qty':     int(row.get('supplier_min_qty', 1) or 1),
            'supplier_url':         row['url'],
            'is_dropshipping':      True,
            'last_synced_at':       now,
        }

        if existing is None:
            sku = _generate_sku()
            full_defaults = {
                'product':              product,
                'name':                 formatted,
                'description':          '',  # description lives on Product
                'is_supplier_fallback': is_fallback,
                'is_active':            (not is_fallback),  # fallback hidden by default
                'price_auto':           True,
                'stock_auto':           True,
                **sync_only_fields,
            }
            variant = ProductVariant.objects.create(
                supplier_sku=supplier_sku,
                sku=sku,
                slug=unique_slug(ProductVariant, slugify(formatted) or sku),
                **full_defaults,
            )
            created = True
        else:
            # Only price/availability/link. No is_active, so that a manually hidden variant
            # is not «revived». No name/desc, so manual edits stay.
            for k, v in sync_only_fields.items():
                setattr(existing, k, v)
            existing.save(update_fields=list(sync_only_fields.keys()) + ['updated_at'])
            variant = existing
            created = False

        # Attributes and characteristics are updated ONLY for new variants.
        if created:
            attr_value_objs = []
            for attr_name, val in v_attrs.items():
                av = get_or_create_attr_value(attr_name, val, attr_cache, value_cache)
                if av:
                    attr_value_objs.append(av)
            variant.attribute_values.set(attr_value_objs)

            attr_names_lc = {n.strip().lower() for n in v_attrs.keys()}
            sort_idx = 0
            for cname, cval in v_chars:
                if not cval or str(cval).strip() in ('', '—', 'nan', 'None'):
                    continue
                if cname.strip().lower() in attr_names_lc:
                    continue  # skip — attribute already carries this
                ProductCharacteristic.objects.create(
                    variant=variant, name=cname, value=str(cval).strip(),
                    sort_order=sort_idx,
                )
                sort_idx += 1
        return variant, created

    # --- images: all on product, dedup by URL --------------------------------

    def _import_images(self, product, group_rows, variant_map, stats):
        """
        Photos are bound to the *colour* of the variant, not to the variant
        itself. All variants that share a colour share one gallery; variants
        without a colour fall back to product-level images (color_value=None).

        Idempotent (safe to re-run):
          - For each colour that already has photos on this product — SKIP.
          - For each colour that doesn't yet have photos — download & attach.
        That way new colours added later (e.g. a new bradas SKU in an existing
        product group) are picked up without re-importing or overwriting the
        existing colours' galleries.
        """

        def variant_color(v):
            if not v:
                return None
            return (v.attribute_values
                    .filter(attribute__is_color=True)
                    .order_by('pk').first())

        # Colours that already have photos — skip them entirely.
        colours_with_photos = set(
            product.images
                   .filter(color_value__isnull=False)
                   .values_list('color_value_id', flat=True)
                   .distinct()
        )
        # Legacy: product also has variant=None, color=None gallery → treat as
        # "no-colour" gallery already imported.
        legacy_unbound = product.images.filter(
            color_value__isnull=True, variant__isnull=True
        ).exists()
        legacy_variant_bound = product.images.filter(variant__isnull=False).exists()

        url_cache       = {}   # url → image.name
        content_cache   = {}   # perceptual hash → image.name
        used_per_color  = {}   # color_value_id|None → {image.name, ...}
        sort_idx_per_color = {}  # color_value_id|None → next sort_order

        def attach_existing(image_name, color_av):
            ckey = color_av.pk if color_av else None
            used = used_per_color.setdefault(ckey, set())
            if image_name in used:
                stats['images_dedup_in_product'] = stats.get('images_dedup_in_product', 0) + 1
                return False
            used.add(image_name)
            sort_idx = sort_idx_per_color.get(ckey, 0)
            img = ProductImage(
                product=product, color_value=color_av,
                sort_order=sort_idx, is_main=(sort_idx == 0),
            )
            img.image = image_name
            img.save()
            sort_idx_per_color[ckey] = sort_idx + 1
            return True

        for row, _, _ in group_rows:
            variant = variant_map.get(row.get('supplier_sku'))
            color_av = variant_color(variant)

            # Skip if this colour (or product-level gallery) already has photos.
            ckey = color_av.pk if color_av else None
            if ckey is None:
                if legacy_unbound or legacy_variant_bound:
                    continue
            elif ckey in colours_with_photos:
                continue
            for url in (row.get('photos') or []):
                if url in url_cache:
                    if attach_existing(url_cache[url], color_av):
                        stats['images_reused_url'] = stats.get('images_reused_url', 0) + 1
                    continue

                result = download_image(url)
                if not result:
                    stats['images_failed'] += 1
                    continue
                fname, content = result

                raw = content.read()
                cleaned, was_modified = strip_corner_label(raw, fname)
                if was_modified:
                    raw = cleaned
                    from django.core.files.base import ContentFile
                    content = ContentFile(cleaned)
                    stats['images_label_stripped'] = stats.get('images_label_stripped', 0) + 1
                else:
                    content.seek(0)

                h = perceptual_hash(raw) or hashlib.md5(raw).hexdigest()
                if '|' in h:
                    near = next((existing for existing in content_cache
                                 if '|' in existing and near_duplicate(existing, h)), None)
                    if near is not None:
                        h = near
                if h in content_cache:
                    existing_name = content_cache[h]
                    url_cache[url] = existing_name
                    if attach_existing(existing_name, color_av):
                        stats['images_reused_content'] = stats.get('images_reused_content', 0) + 1
                    continue

                ckey = color_av.pk if color_av else None
                sort_idx = sort_idx_per_color.get(ckey, 0)
                img = ProductImage(
                    product=product, color_value=color_av,
                    sort_order=sort_idx, is_main=(sort_idx == 0),
                )
                img.image.save(fname, content, save=True)
                used_per_color.setdefault(ckey, set()).add(img.image.name)
                sort_idx_per_color[ckey] = sort_idx + 1
                url_cache[url]     = img.image.name
                content_cache[h]   = img.image.name
                stats['images_downloaded'] += 1

    def _save_image(self, *args, **kwargs):
        """Kept for backwards-compat with possible callers; no-op now."""
        return

    # --- second pass: re-fetch photos for products that have none -----------

    def _refetch_missing_photos(self, stats):
        """
        Walk drop-products that ended up with 0 photos, re-parse their
        supplier_url, and run the same image pipeline.  Sequential (no
        threadpool) — that's exactly what avoids the rate limiting that
        caused the empty pages on the first pass.
        """
        from django.db.models import Count
        candidates = list(Product.objects
                          .filter(variants__is_dropshipping=True)
                          .annotate(n=Count('images'))
                          .filter(n=0)
                          .distinct())
        if not candidates:
            return
        self.stdout.write(f'\n>>> Re-fetching photos for {len(candidates)} products without images...')
        recovered = 0
        for product in candidates:
            v = (product.variants
                        .filter(is_dropshipping=True, supplier_url__startswith='http')
                        .first())
            if not v or not v.supplier_url:
                continue
            desc, specs, stock, photos = get_product_details(v.supplier_url)
            if not photos:
                continue
            # Inject the photos into a synthetic group_rows structure that
            # _import_images expects.  We reuse the existing variant.
            row = {'photos': photos, 'supplier_sku': v.supplier_sku}
            self._import_images(product, [(row, {}, [])], {v.supplier_sku: v}, stats)
            if product.images.exists():
                recovered += 1
        stats['photos_recovered_pass2'] = recovered
        self.stdout.write(f'  Recovered photos for {recovered} / {len(candidates)} products')
