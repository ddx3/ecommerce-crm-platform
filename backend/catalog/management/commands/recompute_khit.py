"""
Recomputes the auto-label "Hit" on the principle of "what sells well RIGHT NOW".

Algorithm (unlike the old "top-20 of all time"):

1. WINDOW. Only orders from the last `--window` days are taken (default=45).
   A hit is current demand, not a legend from two years ago. Old sales
   naturally "fade away", new products with sharp demand have time to slip in.

2. METRIC — the number of DISTINCT orders in which the variant appears
   (`Count(order, distinct=True)`), not the total quantity. One wholesale
   order for 50 pcs = 1 point, not 50 — protection against inflation by a single line.
   `quantity` remains only as a tie-breaker when the number of orders is equal.

3. THRESHOLD. A variant must have at least `--min-orders` orders (default=2),
   otherwise with low traffic one random purchase makes a product a "hit".

4. PER CATEGORY. Take the top `--per-category` (default=5) in EACH category.
   A product in several categories competes in each of them. This way hits are spread
   evenly across the catalog instead of clumping in one popular category.
   Completed = `status='delivered'` OR `payment_status='paid'`.

5. AVAILABILITY. Only what can be BOUGHT now becomes a hit: an active variant
   of an active product, not a raw fallback, and in stock (stock>0) OR
   hybrid/dropshipping (they always ship). A sold-out own variant
   is not marked as a hit — the next recompute will bring it back once it is restocked.

Manual labels (is_auto=False) are never touched.

Run: `python manage.py recompute_khit` (cron once a day).
"""
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Sum, Count, Q
from django.utils import timezone

from catalog.models import Label, ProductVariant, VariantLabel
from orders.models import OrderItem


DEFAULT_WINDOW_DAYS = 45
DEFAULT_PER_CATEGORY = 5
DEFAULT_MIN_ORDERS = 2


class Command(BaseCommand):
    help = 'Recomputes the auto-label "Hit": top-N by number of orders in each category over the last N days.'

    def add_arguments(self, parser):
        parser.add_argument('--window', type=int, default=DEFAULT_WINDOW_DAYS,
                            help=f'Over how many recent days to count sales (default={DEFAULT_WINDOW_DAYS}).')
        parser.add_argument('--per-category', type=int, default=DEFAULT_PER_CATEGORY,
                            help=f'How many hits per category (default={DEFAULT_PER_CATEGORY}).')
        parser.add_argument('--min-orders', type=int, default=DEFAULT_MIN_ORDERS,
                            help=f'Minimum number of distinct orders to qualify as a hit (default={DEFAULT_MIN_ORDERS}).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Do not change the DB, only show which ones would be marked.')

    def handle(self, *args, **opts):
        window = opts['window']
        per_cat = opts['per_category']
        min_orders = opts['min_orders']
        dry = opts['dry_run']

        khit = Label.objects.filter(slug=Label.SLUG_KHIT).first()
        if khit is None:
            self.stderr.write('The khit label was not found in the DB (system_labels migration not applied?).')
            return

        since = timezone.now() - timedelta(days=window)

        # 1+2+3: candidates — variants with >= min_orders distinct completed orders in the window.
        agg = (
            OrderItem.objects
            .filter(product_variant__isnull=False, order__created_at__gte=since)
            .filter(Q(order__status='delivered') | Q(order__payment_status='paid'))
            .values('product_variant_id')
            .annotate(orders=Count('order', distinct=True), qty=Sum('quantity'))
            .filter(orders__gte=min_orders)
        )
        score = {r['product_variant_id']: (r['orders'], r['qty'] or 0) for r in agg}

        winners = set()
        if score:
            # 5: keep only the variants that are available to buy.
            variants = (
                ProductVariant.objects
                .filter(id__in=score.keys(), is_active=True, product__is_active=True,
                        is_supplier_fallback=False)
                .select_related('product')
                .prefetch_related('product__categories')
            )
            buyable = {}
            no_category = 0
            for v in variants:
                available = v.is_dropshipping or v.is_hybrid or (v.stock or 0) > 0
                if not available:
                    continue
                cat_ids = [c.id for c in v.product.categories.all()]
                if not cat_ids:
                    no_category += 1
                    continue
                buyable[v.id] = (v, cat_ids)

            # 4: top-per-category. A variant is a hit if it made the top of at least one of its
            # categories.
            by_cat = defaultdict(list)  # cat_id -> [variant_id, ...]
            for vid, (v, cat_ids) in buyable.items():
                for cid in cat_ids:
                    by_cat[cid].append(vid)

            for cid, vids in by_cat.items():
                vids.sort(key=lambda i: score[i], reverse=True)  # (orders, qty) desc
                winners.update(vids[:per_cat])

            self._report(winners, buyable, score, window, per_cat, min_orders, no_category)
        else:
            self.stdout.write('Products: no variant passed the threshold.')

        if dry:
            self.stdout.write(self.style.WARNING('Dry-run (products) — nothing changed.'))
            self._recompute_bundles(khit, since, per_cat, min_orders, dry=True)
            return

        added, removed = self._sync_labels(khit, winners)
        b_added, b_removed = self._recompute_bundles(khit, since, per_cat, min_orders, dry=False)
        self.stdout.write(self.style.SUCCESS(
            f'Done. Products: +{added}/-{removed} | Bundles: +{b_added}/-{b_removed} auto-khit.'
        ))

    def _recompute_bundles(self, khit, since, per_cat, min_orders, dry):
        """The "Hit" label for BUNDLES — based on the bundle's OWN sales (not its component products). Same
        logic as for products: top per category by number of distinct completed orders in the
        window, only items available to buy."""
        from catalog.models import Bundle
        from orders.models import OrderItem
        agg = (
            OrderItem.objects
            .filter(bundle__isnull=False, order__created_at__gte=since)
            .filter(Q(order__status='delivered') | Q(order__payment_status='paid'))
            .values('bundle_id')
            .annotate(orders=Count('order', distinct=True), qty=Sum('quantity'))
            .filter(orders__gte=min_orders)
        )
        score = {r['bundle_id']: (r['orders'], r['qty'] or 0) for r in agg}
        if not score:
            self.stdout.write('Bundles: none passed the threshold.')
            return self._sync_bundle_labels(khit, set()) if not dry else (0, 0)

        bundles = (
            Bundle.objects
            .filter(id__in=score.keys(), is_active=True)
            .prefetch_related('categories', 'items__product_variant__bulk_source')
        )
        by_cat = defaultdict(list)
        for b in bundles:
            if (b.get_stock() or 0) <= 0:
                continue
            cat_ids = [c.id for c in b.categories.all()]
            if not cat_ids:
                continue  # no category — does not take part in top-per-category (same as products)
            for cid in cat_ids:
                by_cat[cid].append(b.id)

        winners = set()
        for cid, ids in by_cat.items():
            ids.sort(key=lambda i: score[i], reverse=True)
            winners.update(ids[:per_cat])

        self.stdout.write(f'Hit bundles selected: {len(winners)}')
        if dry:
            return (0, 0)
        return self._sync_bundle_labels(khit, winners)

    def _sync_bundle_labels(self, khit, winner_ids):
        """khit on bundles is purely automatic (Bundle.labels without is_auto):
        add to the winners, remove from the rest."""
        from catalog.models import Bundle
        added = 0
        for b in Bundle.objects.filter(id__in=winner_ids):
            if not b.labels.filter(pk=khit.pk).exists():
                b.labels.add(khit)
                added += 1
        removed = 0
        for b in Bundle.objects.filter(labels=khit).exclude(id__in=winner_ids):
            b.labels.remove(khit)
            removed += 1
        return (added, removed)

    def _sync_labels(self, khit, winner_ids):
        added = 0
        for vid in winner_ids:
            _, created = VariantLabel.objects.get_or_create(
                variant_id=vid, label=khit,
                defaults={'expires_at': None, 'is_auto': True},
            )
            if created:
                added += 1
        removed = (
            VariantLabel.objects
            .filter(label=khit, is_auto=True)
            .exclude(variant_id__in=winner_ids)
            .delete()
        )[0]
        return added, removed

    def _report(self, winners, buyable, score, window, per_cat, min_orders, no_category):
        self.stdout.write(
            f'Window: {window} d | top-{per_cat}/category | threshold: {min_orders} orders'
        )
        self.stdout.write(f'Hits selected: {len(winners)}')
        for vid in sorted(winners, key=lambda i: score[i], reverse=True):
            v = buyable[vid][0]
            orders, qty = score[vid]
            self.stdout.write(f'  v{vid} orders={orders} pcs={qty} | {v.product.name[:60]}')
        if no_category:
            self.stdout.write(self.style.WARNING(
                f'Skipped {no_category} variant(s) without a category — they do not take part in top-per-category.'
            ))
