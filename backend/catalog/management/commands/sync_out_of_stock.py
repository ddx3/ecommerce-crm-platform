# -*- coding: utf-8 -*-
"""Catches up the "sold out" mark across the whole catalog.

The mark is set by signals and a hook in order write-off, but stock is sometimes written bypassing
them (bulk import, direct SQL). The command reconciles the state and fixes discrepancies — it can be
run at any time, it is idempotent.

Without --apply it only shows the planned changes (dry-run)."""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import ProductVariant, sync_out_of_stock_mark


class Command(BaseCommand):
    help = 'Reconciles the "sold out" mark with the actual stock level.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Execute. Without the flag, only show (dry-run).')

    def handle(self, *args, **opts):
        planned = []
        for v in ProductVariant.objects.select_related('product').iterator(chunk_size=500):
            was = v.out_of_stock_at
            if sync_out_of_stock_mark(v, commit=False):
                planned.append((v, was, v.out_of_stock_at))

        for v, was, now in planned:
            self.stdout.write('  %-9s %-6s stock=%-5s %s' % (
                'MARK' if now else 'CLEAR',
                v.sku or '—', v.stock or 0, (v.name or '')[:60],
            ))

        if not planned:
            self.stdout.write(self.style.SUCCESS('The "sold out" marks are already correct.'))
            return

        if not opts['apply']:
            self.stdout.write(self.style.WARNING(
                'DRY-RUN. Will change: %s. Run with --apply.' % len(planned)))
            return

        with transaction.atomic():
            for v, _, now in planned:
                ProductVariant.objects.filter(pk=v.pk).update(out_of_stock_at=now)
        self.stdout.write(self.style.SUCCESS('Packagings updated: %s' % len(planned)))
