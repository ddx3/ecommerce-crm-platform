# -*- coding: utf-8 -*-
"""Repair of "out of stock" for own packagings: the variant has a "Packaging size"
(unit_size)
and the product HAS a bulk bag (BulkSource), but there is no link between them (bulk_source=None) —
so the
stock is not computed from the bag and stays 0/stale.

Finds such orphans, links them to the product's bag and recalculates the stock
(bag.quantity // effective_unit(unit_size)). Manual stock (stock_manual=True) is left untouched.

Without --apply it is only a dry-run (shows what will change and how, including the new stock)."""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import ProductVariant, BulkSource


class Command(BaseCommand):
    help = 'Links own packagings with unit_size to the existing bulk bag of the product and recalculates the stock.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Perform the linking. Without the flag, only show (dry-run).')

    def handle(self, *args, **opts):
        apply = opts['apply']
        orphans = (ProductVariant.objects
                   .filter(unit_size__isnull=False, bulk_source__isnull=True)
                   .exclude(unit_size=0)
                   .select_related('product')
                   .order_by('product__name', 'unit_size'))

        rows = []
        for v in orphans:
            bag = BulkSource.objects.filter(product_id=v.product_id).order_by('pk').first()
            if bag is None:
                continue  # no bag — nothing to link to (manual stock is needed)
            eu = bag.effective_unit(v.unit_size)
            new_stock = int(bag.quantity // eu) if eu and eu > 0 else 0
            rows.append((v, bag, new_stock))

        if not rows:
            self.stdout.write('No orphans found (unit_size without a bag, while the product has a bag).')
            return

        for v, bag, new_stock in rows:
            manual = ' [MANUAL — stock left untouched]' if v.stock_manual else ''
            self.stdout.write(
                f'  id={v.id} "{v.name}" | {v.unit_size} g | bag #{bag.id} ({bag.quantity} g) '
                f'| stock {v.stock} → {v.stock if v.stock_manual else new_stock}{manual}'
            )

        if not apply:
            self.stdout.write(self.style.WARNING(
                f'\nDRY-RUN: {len(rows)} variant(s). Run with --apply to link them.'))
            return

        changed = 0
        with transaction.atomic():
            for v, bag, _ in rows:
                v.bulk_source = bag  # save() recalculates stock itself (except stock_manual)
                v.save(update_fields=['bulk_source'])
                changed += 1
        self.stdout.write(self.style.SUCCESS(f'\nLinked: {changed} variant(s).'))
