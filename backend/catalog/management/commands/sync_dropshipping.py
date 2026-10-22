"""
Daily sync with bradas.ua:
  - For every supplier_sku in current bradas listings — update price + stock.
  - SKU appeared on bradas, missing from DB:
      * fits an existing Product group → add as a new Variant (active if in stock)
      * brand-new group → create Product+Variant inactive (admin reviews)
  - SKU disappeared from bradas → variant.is_active=False, stock=0.
  - SKU absent for >--prune-after-days AND not referenced by any OrderItem
    → delete the variant outright.
  - Significant price change (>20%) is logged as an event.

All changes are logged into DropSyncEvent (visible in admin → "Sync events").

Usage:
    python manage.py sync_dropshipping              # full daily run
    python manage.py sync_dropshipping --dry-run    # only print, no changes
    python manage.py sync_dropshipping --prune-after-days 60
"""
import re
from datetime import timedelta
from decimal import Decimal

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    Brand, DropSyncEvent, Product, ProductVariant,
)
from catalog.management.commands.import_dropshipping import (
    BRAND_NAME, CATEGORY_CONFIGS, HEADERS,
    download_image, get_or_create_attr_value,
    get_or_create_category_path, get_product_details,
    perceptual_hash, near_duplicate, strip_corner_label,
    round_down_5, _generate_sku,
)
from slugify import slugify
from catalog.models import ProductCharacteristic, ProductImage
from catalog.models import unique_slug


# Share of existing dropshipping variants that may 'disappear' from bradas in a single run without
# suspecting a failure.
MAX_GONE_SHARE = 0.3


# === Quick listing parser (no detail-page fetch) =============================

PRICE_RE = re.compile(r'[\d.,]+')


def parse_listing_quick(url):
    """
    Walk a category listing page and return one dict per card with just
    enough data for a price/stock sync — no detail-page round trips.
    Matched against DB by `supplier_url` (stable), not by SKU.
    """
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.encoding = 'utf-8'
    soup = BeautifulSoup(r.text, 'html.parser')
    lst = soup.find_all('div', class_='bxr-list')
    if not lst:
        return []
    out = []
    for item in lst[0].find_all('div', class_='bxr-ecommerce-v3-lite'):
        try:
            name_tag = item.find('div', class_='bxr-element-name').find('a')
            name = name_tag.get_text(strip=True)
            href = name_tag.get('href') or ''
            link = href if href.startswith('http') else 'https://bradas.ua' + href

            price_value = 0.0
            pt = item.find('span', class_='bxr-market-current-price')
            if pt:
                cleaned = re.sub(r'[^\d.,]', '', pt.get_text(' ', strip=True))
                cleaned = cleaned.replace(',', '.').strip('.')
                if cleaned:
                    try:
                        price_value = float(cleaned)
                    except ValueError:
                        pass

            stock = 0
            qty_attr = item.find(attrs={'data-quantity': True})
            if qty_attr:
                try:
                    stock = int(qty_attr.get('data-quantity'))
                except (TypeError, ValueError):
                    stock = 0

            if not link:
                continue
            out.append({
                'name':           name,
                'supplier_price': price_value,
                'stock':          stock,
                'url':            link,
            })
        except Exception:
            continue
    return out


# === Command ==================================================================

class Command(BaseCommand):
    help = 'Daily sync of the dropshipping catalog with bradas.ua.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would be done, change nothing')
        parser.add_argument('--prune-after-days', type=int, default=30,
                            help='After how many days of inactivity to finally delete (default: 30)')
        parser.add_argument('--price-jump-threshold', type=float, default=0.2,
                            help='Threshold for logging a price change (a fraction, default: 0.2 = 20%%)')
        parser.add_argument('--skip-fallback', action='store_true',
                            help='Do not create products that failed the SKU format '
                                 '(skip them instead of creating them inactive).')

    def handle(self, *args, **opts):
        # Windows: the cp1252 console cannot print Cyrillic → switch to UTF-8.
        import sys
        for stream in (sys.stdout, sys.stderr):
            try:
                if hasattr(stream, 'reconfigure'):
                    stream.reconfigure(encoding='utf-8')
            except Exception:
                pass

        self.dry = opts['dry_run']
        self.prune_days = opts['prune_after_days']
        self.price_jump = opts['price_jump_threshold']
        self.skip_fallback = opts['skip_fallback']
        now = timezone.now()

        if self.dry:
            self.stdout.write(self.style.WARNING('=== DRY-RUN: no changes will be persisted ==='))

        # --- Step 1: collect everything bradas currently sells ---
        # Match by supplier_url (stable identifier set during initial import)
        # rather than SKU — SKU extraction from listing pages is unreliable.
        bradas = {}  # supplier_url → {category_key, ...}
        failed = []  # categories that could not be read (network, bot protection, new markup)
        for cat_key, cfg in CATEGORY_CONFIGS.items():
            self.stdout.write(f'Listing {cat_key}...')
            try:
                items = parse_listing_quick(cfg['url'])
            except Exception as e:
                self.stderr.write(f'  Failed to fetch {cat_key}: {e}')
                failed.append(cat_key)
                continue
            if not items:
                self.stderr.write(f'  {cat_key}: the listing is empty (failure, bot protection or new markup)')
                failed.append(cat_key)
                continue
            for it in items:
                it['category_key'] = cat_key
                bradas[it['url']] = it
            self.stdout.write(f'  {len(items)} items')

        if not bradas:
            self.stdout.write(self.style.ERROR('No listings parsed — aborting'))
            return

        # --- Step 2: existing drop variants in DB ---
        existing = {
            v.supplier_url: v
            for v in ProductVariant.objects.filter(is_dropshipping=True).select_related('product')
            if v.supplier_url
        }

        stats = dict(
            updated=0, returned=0, removed=0, deleted=0,
            new_variants=0, new_products=0, price_jumps=0,
            skipped_fallback=0, errors=0,
        )

        # --- Step 3: update existing + flag returned/price-changed ---
        with self._atomic():
            for url, info in bradas.items():
                v = existing.get(url)
                if not v:
                    continue
                try:
                    self._update_variant(v, info, now, stats)
                except Exception as e:
                    stats['errors'] += 1
                    self.stderr.write(f'  ERR updating {url}: {e}')

        # Sync does NOT create new products from bradas — import_dropshipping exists for that.
        # Sync is only a quick update of prices/stock of existing variants.
        new_urls = [u for u in bradas if u not in existing]
        if new_urls:
            stats['new_on_bradas_seen'] = len(new_urls)
            self.stdout.write(
                f"{len(new_urls)} new items appeared on bradas. "
                f"Sync does not import them — run import_dropshipping if needed."
            )

        # --- Disappeared from bradas: deactivate (exception: "discontinued").
        # ---
        gone = set(existing) - set(bradas)
        # But not when the listing may have been only partially read or suspiciously many
        # 'disappeared': a network failure or new markup
        # would disable a whole category, and the cleanup below would delete it permanently after
        # prune_days (the "Supplier changes" section is hidden in production, so the manager would
        # not see the events).
        suspicious = bool(failed) or (bool(existing) and len(gone) / len(existing) > MAX_GONE_SHARE)
        if gone and suspicious:
            self.stderr.write(
                f"  Skipping the deactivation of {len(gone)} of {len(existing)} 'disappeared' items and the cleanup: "
                f"{'could not read ' + ', '.join(failed) if failed else 'over ' + str(int(MAX_GONE_SHARE * 100)) + '% disappeared'}"
                f" — looks like a failure, not like items disappearing from bradas."
            )
            stats['errors'] += len(failed) or 1
            gone = set()
        for url in gone:
            try:
                self._deactivate_variant(existing[url], now, stats)
            except Exception as e:
                stats['errors'] += 1
                self.stderr.write(f'  ERR deactivating {url}: {e}')

        # --- Prune: long inactive with no order references — delete. ---
        if not suspicious:
            self._prune_obsolete(now, stats)

        self._report(stats)

        # Partial failure — loudly: the scheduler heartbeat will record the failure (the "Task …
        # failing" card in the Analyst), and the button in the CRM
        # will show an error. Everything that was read successfully has already been saved above.
        if failed:
            from django.core.management.base import CommandError
            raise CommandError('Could not read the bradas categories: ' + ', '.join(failed)
                               + '. The rest was updated; the "disappeared" items were not deactivated.')

    # --- helpers --------------------------------------------------------------

    def _atomic(self):
        """Wrap in a transaction unless dry-run."""
        from contextlib import nullcontext
        return nullcontext() if self.dry else transaction.atomic()

    def _log_event(self, kind, variant=None, sku='', message=''):
        if self.dry:
            self.stdout.write(f'  [DRY] EVENT {kind} sku={sku} msg={message[:80]}')
            return
        DropSyncEvent.objects.create(
            kind=kind, variant=variant,
            supplier_sku=sku, message=message,
        )

    def _log_new_batch(self, stats):
        """Collapse all created new arrivals into ONE summary event.

        Per-item new_product/new_variant events flooded the notifications with hundreds of records
        (catalog replenishment is not a "change" the manager reacts to individually).
        """
        np = stats.get('new_products', 0)
        nv = stats.get('new_variants', 0)
        if self.dry or (np + nv) == 0:
            return
        parts = []
        if np:
            parts.append(f'{np} new products')
        if nv:
            parts.append(f'{nv} new packagings')
        DropSyncEvent.objects.create(
            kind=DropSyncEvent.KIND_NEW_BATCH,
            variant=None, supplier_sku='',
            message='Bradas sync: ' + ', '.join(parts)
                    + '. Created inactive — review and activate them in the catalog.',
        )

    def _update_variant(self, v, info, now, stats):
        # The listing price may be for 1 pc when a batch is mandatory (pack of 50 pcs).
        # The listing does not say so, so we multiply by the stored min. batch —
        # otherwise a pack of pins would cost as much as a single piece.
        raw_price = info['supplier_price']
        if raw_price:
            raw_price = float(raw_price) * max(1, int(v.supplier_min_qty or 1))
        new_price = round_down_5(raw_price) if raw_price else 0
        new_stock = max(0, int(info['stock'] or 0))
        old_price = float(v.price)

        was_inactive = not v.is_active
        v.price = Decimal(str(new_price))
        v.stock = new_stock
        # is_active is NOT touched: activity is controlled by the manager manually. Missing
        # stock ≠ "withdrawn from sale". The product stays in the catalog, the
        # customer sees
        # "out of stock". The manager deactivates on their own what is truly
        # withdrawn.
        # supplier_price is kept in the same units as price — the cost of the batch
        # that actually has to be bought, not of a single piece.
        v.supplier_price = Decimal(str(round(raw_price, 2))) if raw_price else None
        v.last_synced_at = now

        if not self.dry:
            v.save(update_fields=['price', 'stock',
                                  'supplier_price', 'last_synced_at', 'updated_at'])
        stats['updated'] += 1

        # Supplier stock appeared on a previously inactive variant —
        # an informational event for the manager; we do NOT activate it ourselves.
        if was_inactive and new_stock > 0:
            stats['returned'] += 1
            self._log_event(
                DropSyncEvent.KIND_RETURNED, variant=v, sku=v.supplier_sku,
                message=f'{v.product.name} / {v.name}: back in stock at the supplier '
                        f'(stock={new_stock}, price={new_price}). Can be activated.'
            )

        # Significant price jump?
        if old_price > 0 and new_price > 0:
            change = abs(new_price - old_price) / old_price
            if change >= self.price_jump:
                stats['price_jumps'] += 1
                self._log_event(
                    DropSyncEvent.KIND_PRICE_CHANGE, variant=v, sku=v.supplier_sku,
                    message=f'{v.product.name} / {v.name}: '
                            f'price {old_price:.2f} → {new_price:.2f} '
                            f'({change*100:.0f}%)'
                )

    def _handle_new_sku(self, url, info, now, stats):
        """
        New product on bradas:
          - parse detail page (gets a real SKU from the "Артикул" (SKU) prop of the supplier page);
          - run the category's group_fn → if its group_key matches an existing
            Product → add as Variant; else create a new Product (inactive).
        """
        cfg = CATEGORY_CONFIGS.get(info['category_key'])
        if not cfg:
            return
        # Enrich with detail page — that's where the SKU lives.
        desc, specs, stock, photos = get_product_details(info['url'])
        # 'Артикул' (SKU) is the property label on the Ukrainian supplier page: scraper data, kept as is.
        sku = (specs.get('Артикул', '') or info['name'].split()[-1] if info['name'] else '').strip()
        if not sku:
            return  # can't track without a SKU
        # Skip if already imported (race / dedup safety)
        if ProductVariant.objects.filter(supplier_sku=sku).exists():
            return
        row = {
            'supplier_sku':   sku,
            'name':           info['name'],
            'supplier_price': info['supplier_price'],
            'stock':          info['stock'] if info['stock'] is not None else stock,
            'url':            info['url'],
            'description':    desc,
            'specs':          specs,
            'photos':         photos,
        }

        groups = cfg['group_fn']([row])
        if not groups:
            return
        group_key, group_rows = next(iter(groups.items()))
        formatted_row, v_attrs, v_chars = group_rows[0]
        is_fallback = bool(formatted_row.get('_is_fallback'))

        # Fallback (failed the SKU format) is never created — the catalog stays clean.
        # The --skip-fallback flag remains for compatibility but is no longer needed.
        if is_fallback:
            stats['skipped_fallback'] = stats.get('skipped_fallback', 0) + 1
            return

        brand = Brand.objects.filter(name=BRAND_NAME).first()
        if not brand and not self.dry:
            brand = Brand.objects.create(name=BRAND_NAME)

        existing_product = Product.objects.filter(name=group_key, brand=brand).first()
        is_new_product = existing_product is None

        if self.dry:
            kind = 'NEW_PRODUCT' if is_new_product else 'NEW_VARIANT'
            tag = ' [fallback]' if is_fallback else ''
            self.stdout.write(f'  [DRY] {kind}{tag} sku={sku}: "{group_key}" / "{formatted_row["name"]}"')
            stats['new_products' if is_new_product else 'new_variants'] += 1
            return

        if is_new_product:
            category = get_or_create_category_path(cfg['category_path'], {})
            product = Product.objects.create(
                name=group_key,
                slug=unique_slug(Product, slugify(group_key) or 'dropshipping'),
                description=desc,
                brand=brand,
                # Active iff we successfully formatted it (not fallback).
                is_active=(not is_fallback),
            )
            if category:
                product.categories.add(category)
            stats['new_products'] += 1
        else:
            product = existing_product
            stats['new_variants'] += 1

        # Variant. The min. batch is known from the detail page (see above) — the price
        # on bradas may be for 1 pc with a mandatory pack.
        min_qty = max(1, int(specs.get('_min_qty', 1) or 1))
        raw_supplier = float(info['supplier_price']) * min_qty if info['supplier_price'] else 0
        our_price = round_down_5(raw_supplier) if raw_supplier else 0
        new_stock = max(0, int(info['stock'] or 0))
        variant = ProductVariant.objects.create(
            sku=_generate_sku(),
            slug=unique_slug(ProductVariant, slugify(formatted_row['name']) or sku),
            supplier_sku=sku,
            product=product,
            name=formatted_row['name'],
            description='',
            price=Decimal(str(our_price)),
            stock=new_stock,
            supplier_price=Decimal(str(round(raw_supplier, 2))) if raw_supplier else None,
            supplier_min_qty=min_qty,
            supplier_url=info['url'],
            is_dropshipping=True,
            is_supplier_fallback=is_fallback,
            # Hidden if fallback OR brand-new product (admin needs to approve).
            is_active=(not is_fallback and not is_new_product),
            price_auto=True,
            stock_auto=True,
            last_synced_at=now,
        )

        # Attributes + characteristics
        attr_cache = {}; value_cache = {}
        for attr_name, val in v_attrs.items():
            av = get_or_create_attr_value(attr_name, val, attr_cache, value_cache)
            if av:
                variant.attribute_values.add(av)
        for i, (cname, cval) in enumerate(v_chars):
            if cval and str(cval) not in ('', '—', 'nan', 'None'):
                ProductCharacteristic.objects.create(
                    variant=variant, name=cname, value=str(cval), sort_order=i,
                )

        # Photos — for a new product or a new color in an existing product.
        # _import_images itself skips colors that already have photos.
        if photos:
            from catalog.management.commands.import_dropshipping import Command as ImportCmd
            importer = ImportCmd()
            img_stats = {
                'images_downloaded': 0, 'images_failed': 0,
                'images_dedup_in_product': 0, 'images_reused_url': 0,
                'images_reused_content': 0, 'images_label_stripped': 0,
            }
            group_rows_for_imgs = [({
                'supplier_sku': sku,
                'url': info['url'],
                'photos': photos,
                'description': desc,
                'specs': specs,
            }, v_attrs, v_chars)]
            importer._import_images(product, group_rows_for_imgs, {sku: variant}, img_stats)

        # After adding a new variant we recompute "shared vs different":
        # an attribute that is now the same across all variants moves to the characteristics.
        from catalog.management.commands.import_dropshipping import (
            normalize_variant_attrs_to_characteristics,
        )
        normalize_variant_attrs_to_characteristics(product)

        # We do NOT log the event per item — new arrivals are collapsed into one summary
        # event at the end of the run (_log_new_batch).

    def _save_product_photos(self, product, urls):
        """Reuse the import logic for new-product photos (with NEW-label strip)."""
        from django.core.files.base import ContentFile
        seen_urls = set()
        seen_hashes = set()
        sort_idx = 0
        for url in urls:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            res = download_image(url)
            if not res:
                continue
            fname, content = res
            raw = content.read()
            cleaned, mod = strip_corner_label(raw, fname)
            if mod:
                raw = cleaned
                content = ContentFile(cleaned)
            else:
                content.seek(0)
            h = perceptual_hash(raw)
            if h and any(near_duplicate(h, e) for e in seen_hashes):
                continue
            if h:
                seen_hashes.add(h)
            img = ProductImage(
                product=product, variant=None,
                sort_order=sort_idx, is_main=(sort_idx == 0),
            )
            img.image.save(fname, content, save=True)
            sort_idx += 1

    def _deactivate_variant(self, v, now, stats):
        if not v.is_active and v.stock == 0:
            return  # already deactivated, just touch sync time
        if self.dry:
            self.stdout.write(f'  [DRY] DEACTIVATE {v.supplier_sku} ({v.name})')
            stats['removed'] += 1
            return
        v.is_active = False
        v.stock = 0
        v.last_synced_at = now
        v.save(update_fields=['is_active', 'stock', 'last_synced_at', 'updated_at'])
        stats['removed'] += 1
        self._log_event(
            DropSyncEvent.KIND_REMOVED, variant=v, sku=v.supplier_sku,
            message=f'{v.product.name} / {v.name}: disappeared from bradas.'
        )

    def _prune_obsolete(self, now, stats):
        cutoff = now - timedelta(days=self.prune_days)
        candidates = ProductVariant.objects.filter(
            is_dropshipping=True, is_active=False,
            last_synced_at__isnull=False, last_synced_at__lt=cutoff,
        )
        from orders.models import OrderItem
        for v in candidates:
            if OrderItem.objects.filter(product_variant=v).exists():
                continue  # keep — referenced by an order
            sku = v.supplier_sku
            name = f'{v.product.name} / {v.name}'
            if self.dry:
                self.stdout.write(f'  [DRY] DELETE {sku} ({name})')
                stats['deleted'] += 1
                continue
            v.delete()
            stats['deleted'] += 1
            self._log_event(
                DropSyncEvent.KIND_DELETED, variant=None, sku=sku,
                message=f'{name}: permanently deleted '
                        f'(absent from bradas for >{self.prune_days} days).'
            )

    def _report(self, stats):
        # Ensure all bradas drops are linked to the Bradas supplier
        # (new variants created by this sync get the supplier).
        if not self.dry:
            from catalog.models import Supplier
            brd = Supplier.objects.filter(code='bradas').first()
            if brd:
                ProductVariant.objects.filter(
                    is_dropshipping=True, supplier__isnull=True,
                ).update(supplier=brd)
        self.stdout.write(self.style.SUCCESS('\nSync complete:'))
        for k, v in stats.items():
            self.stdout.write(f'  {k}: {v}')
        if not self.dry and (stats['new_products'] or stats['new_variants']
                             or stats['removed'] or stats['price_jumps']):
            unread = DropSyncEvent.objects.filter(is_seen=False).count()
            self.stdout.write(f'  Unread events in admin: {unread}')
