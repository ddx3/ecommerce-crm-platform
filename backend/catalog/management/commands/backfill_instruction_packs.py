# -*- coding: utf-8 -*-
"""Backfill pack sizes into already-made instruction markup.

`instruction_map.packs` appeared later than the markup itself. Without it, when a product in a
bundle is replaced ENTIRELY, the old variant can no longer be retrieved, and the 'dose in text =
package' check takes an arbitrary pack size of the same product ("Liposam 280 ml" instead of "10
ml") — the warning comes out wrong.

We fill from the CURRENT composition and only for components that are still in the composition: if
the product has already been replaced, its pack size cannot be recovered from anywhere, and we won't
guess. The command is idempotent: it does not touch existing values.
"""
from django.core.management.base import BaseCommand

from catalog import instruction_resolve as ir
from catalog.models import Bundle


class Command(BaseCommand):
    help = 'Backfill instruction_map.packs into existing marked-up instructions'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        dry = opts['dry_run']
        touched = 0
        for b in Bundle.objects.exclude(instruction_map=None):
            m = b.instruction_map or {}
            if not m.get('components'):
                continue
            packs = dict(m.get('packs') or {})
            for c in ir.components_of_template(b):
                if not c.variant:
                    continue
                key = str(c.variant.product_id)
                if key not in packs and c.pack:
                    packs[key] = c.pack
            if packs == (m.get('packs') or {}):
                continue
            touched += 1
            self.stdout.write(f'bundle{b.pk}: +{len(packs) - len(m.get("packs") or {})} packs')
            if dry:
                continue
            m['packs'] = packs
            Bundle.objects.filter(pk=b.pk).update(instruction_map=m)
        self.stdout.write(self.style.SUCCESS(
            f'{"checked" if dry else "updated"} packs in {touched} instructions'))
