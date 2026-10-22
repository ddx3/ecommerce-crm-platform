"""
READ-ONLY reconciliation of actual stock levels. Changes NOTHING — only counts discrepancies
and prints a report. Safe to run in production.

Checks the invariants that stock accounting must hold:
  1. Bag↔stock: for own packagings (bulk_source + unit_size, not manual) the real
     variant.stock must equal floor(bag.quantity / effective_unit) — this is exactly how
     sync_variants / ProductVariant.save compute it. A mismatch = drift.
  2. Orphaned/empty bags: a variant shows bag accounting but there is no bag (SET_NULL),
     or bag.quantity < 0 / None.
  3. 'Eaten' stock: OrderItem.stock_applied=True on a cancelled order — stock was written off,
     but the order is dead (it should have been returned).
  4. Missed write-off: a physical item with stock_applied=False where stock SHOULD have been
     held (_stock_consumable=True) — sold, but stock did not drop.
  5. Broken write-off pointer: stock_applied=True, stock_consumed_bag_id → a nonexistent
     bag (an exact return is no longer possible).

Examples: `python manage.py audit_stock`  (short report)
          `python manage.py audit_stock --limit 50`  (more sample rows)
"""
from django.core.management.base import BaseCommand
from django.db.models import Min

from catalog.models import BulkSource
from catalog.stock_audit import scan_bag_drift
from orders.models import OrderItem


def _stock_consumable(order):
    """Mirrors orders.models._stock_consumable (without importing a private symbol)."""
    if order is None or order.status == 'cancelled':
        return False
    if order.payment_method == 'monobank' and order.payment_status != 'paid':
        return False
    return True


class Command(BaseCommand):
    help = 'READ-ONLY stock reconciliation: bag↔stock drift, eaten/not-written-off stock. Changes nothing.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25,
                            help='How many sample rows to show per category (default 25).')

    def _h(self, text):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(text))

    def handle(self, *args, **opts):
        limit = opts['limit']

        # The stock_applied/stock_consumed mechanism appeared in migration 0039 — orders created
        # BEFORE it never went through _consume (stock_applied=False is normal there, not a bug).
        # Dynamic threshold: the earliest order that ACTUALLY has a written-off item. Checks 3/4 are
        # computed only from that moment — otherwise all the pre-reform history is noise.
        cutoff = OrderItem.objects.filter(stock_applied=True).aggregate(
            m=Min('order__created_at'))['m']

        # Synthetic orders from analytics tests (bulk-created, did not go through _consume) are not
        # real sales — exclude them from the write-off checks.
        TEST_SOURCES = ['__ANALYTICS_TEST__']

        # ── 1+2. Bag ↔ variant.stock ──────────────────────────────────────
        # The invariant itself lives in catalog/stock_audit.py — shared with heal_stock_drift,
        # so that the audit and the self-healing do not diverge on the definition of the 'correct'
        # stock.
        self._h('1) Bag ↔ variant.stock drift (own packagings)')
        drift, no_bag, checked = scan_bag_drift()

        self.stdout.write(f'   Bag-based variants checked: {checked}')
        if drift:
            self.stdout.write(self.style.ERROR(f'   ⚠ Stock mismatch in {len(drift)} variants:'))
            for v, exp, act, bag in drift[:limit]:
                self.stdout.write(
                    f'     v#{v.pk} [{v.sku or "—"}] {(v.name or "")[:45]!r}: '
                    f'stock={act}, expected={exp} '
                    f'(bag#{bag.pk} {bag.quantity}{bag.unit}, unit_size={v.unit_size}, waste={bag.waste_percent}%)')
            if len(drift) > limit:
                self.stdout.write(f'     … and {len(drift) - limit} more')
        else:
            self.stdout.write(self.style.SUCCESS('   ✓ No drift'))

        if no_bag:
            self.stdout.write(self.style.WARNING(
                f'   ⚠ {len(no_bag)} variants have unit_size but NO tracked bag:'))
            for v in no_bag[:limit]:
                self.stdout.write(f'     v#{v.pk} [{v.sku or "—"}] {(v.name or "")[:50]!r} stock={v.stock}')
            if len(no_bag) > limit:
                self.stdout.write(f'     … and {len(no_bag) - limit} more')

        # ── negative / empty bags (across the whole table, not only linked ones) ──
        self._h('2) Negative / empty bag balances')
        neg_bags = list(BulkSource.objects.filter(quantity__lt=0))
        if neg_bags:
            self.stdout.write(self.style.ERROR(f'   ⚠ {len(neg_bags)} bags with a negative balance:'))
            for b in neg_bags[:limit]:
                self.stdout.write(f'     bag#{b.pk} "{b.name}" = {b.quantity}{b.unit} (product #{b.product_id})')
        else:
            self.stdout.write(self.style.SUCCESS('   ✓ No negative bags'))

        # ── 3. 'Eaten' stock: written off, but the order is cancelled ────────────
        # is_imported (OLD/Horoshop) is excluded: they are insert-only, never went through the live
        # _consume/_restore — their flags are historical and do not reflect actual write-off.
        self._h('3) Stock written off on CANCELLED orders (should have been returned)')
        orphaned = (OrderItem.objects
                    .filter(stock_applied=True, order__status='cancelled')
                    .exclude(order__is_imported=True)
                    .exclude(order__source__in=TEST_SOURCES)
                    .select_related('order')
                    .exclude(course__isnull=False))
        if cutoff is not None:
            orphaned = orphaned.filter(order__created_at__gte=cutoff)
        orphaned_n = orphaned.count()
        if orphaned_n:
            self.stdout.write(self.style.ERROR(
                f'   ⚠ {orphaned_n} items with stock_applied=True on cancelled orders:'))
            for it in orphaned[:limit]:
                self.stdout.write(
                    f'     order #{it.order.order_number} item#{it.pk} '
                    f'{(it.display_name or "")[:40]!r} x{it.quantity} '
                    f'consumed={it.stock_consumed} bag={it.stock_consumed_bag_id}')
            if orphaned_n > limit:
                self.stdout.write(f'     … and {orphaned_n - limit} more')
        else:
            self.stdout.write(self.style.SUCCESS('   ✓ None'))

        # ── 4. Missed write-off: stock should have been held, but stock_applied=False ─
        self._h('4) Stock not written off where it should have been held (sold, but stock did not drop)')
        candidates = (OrderItem.objects
                      .filter(stock_applied=False)
                      .exclude(course__isnull=False)
                      .exclude(order__status='cancelled')
                      .exclude(order__is_imported=True)
                      .exclude(order__source__in=TEST_SOURCES)
                      .select_related('order'))
        if cutoff is not None:
            candidates = candidates.filter(order__created_at__gte=cutoff)
        missed = [it for it in candidates.iterator()
                  if (it.product_variant_id or it.bundle_id) and _stock_consumable(it.order)]
        if missed:
            self.stdout.write(self.style.ERROR(
                f'   ⚠ {len(missed)} physical items NOT written off although they should have been:'))
            for it in missed[:limit]:
                o = it.order
                self.stdout.write(
                    f'     order #{o.order_number} [{o.status}/{o.payment_method}/{o.payment_status}] '
                    f'item#{it.pk} {(it.display_name or "")[:40]!r} x{it.quantity} '
                    f'drop={it.was_dropshipped}')
            if len(missed) > limit:
                self.stdout.write(f'     … and {len(missed) - limit} more')
        else:
            self.stdout.write(self.style.SUCCESS('   ✓ None'))

        # ── 5. Broken write-off bag pointer ────────────────────────────────
        self._h('5) stock_consumed_bag_id → nonexistent bag (an exact return is impossible)')
        applied_bag_ids = set(
            OrderItem.objects.filter(stock_applied=True, stock_consumed_bag_id__isnull=False)
            .values_list('stock_consumed_bag_id', flat=True))
        existing_bag_ids = set(
            BulkSource.objects.filter(pk__in=applied_bag_ids).values_list('pk', flat=True))
        dangling = applied_bag_ids - existing_bag_ids
        if dangling:
            broken = (OrderItem.objects
                      .filter(stock_applied=True, stock_consumed_bag_id__in=dangling)
                      .exclude(order__is_imported=True)
                      .select_related('order'))
            self.stdout.write(self.style.ERROR(
                f'   ⚠ {broken.count()} items point to {len(dangling)} deleted bags:'))
            for it in broken[:limit]:
                self.stdout.write(
                    f'     order #{it.order.order_number} item#{it.pk} '
                    f'bag={it.stock_consumed_bag_id} consumed={it.stock_consumed}')
        else:
            self.stdout.write(self.style.SUCCESS('   ✓ None'))

        # ── Summary ────────────────────────────────────────────────────────
        self._h('SUMMARY')
        if cutoff is not None:
            self.stdout.write(f'   Threshold for checks 3/4 (first live write-off): {cutoff:%Y-%m-%d %H:%M}')
        # Real stock problems (no_bag is informational, not a write-off defect).
        hard = len(drift) + len(neg_bags) + orphaned_n + len(missed) + len(dangling)
        line = (f'drift={len(drift)}  negative_bags={len(neg_bags)}  eaten={orphaned_n}  '
                f'not_written_off={len(missed)}  broken_bag_ids={len(dangling)}')
        if hard:
            self.stdout.write(self.style.ERROR(f'   ⚠ Real problems: {line}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'   ✓ No real problems: {line}'))
        self.stdout.write(f'   Informational: no_tracked_bag={len(no_bag)} '
                          f'(the write-off correctly comes from variant.stock — not a defect)')
        self.stdout.write(self.style.WARNING('   (READ-ONLY: nothing changed)'))
