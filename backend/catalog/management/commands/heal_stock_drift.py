"""Reconciliation and healing of the 'bag ↔ variant.stock' drift for own packagings.

Why: the stock of a bag-based packaging is DERIVED from the bag's balance, which is maintained by a
scatter of recalculations (post_save signal, sync_variants, ProductVariant.save, the supplier
invoice and its revert, order write-off/return). If any of them breaks halfway, a lie stays in the
DB, and the storefront shows it to the customer. This is how a product with a full bag hung as "out
of stock" until someone noticed it by hand.

THE DIRECTION DECIDES EVERYTHING (see split_by_direction in catalog/stock_audit.py):
  down — the storefront shows more than the bag gives → a live oversell risk, we always heal;
  up   — the recalculation will put the product on sale; correct only if the bag is not inflated.
         The machine does not know this, so we do NOT raise automatically: we write ERROR to the log
         and
         leave it to a human. To raise deliberately: --allow-raise (with --bags if needed).

Manual stock (stock_manual=True) is not touched — it is deliberately detached from the bag.

  python manage.py heal_stock_drift                      # auto-heal (down only)
  python manage.py heal_stock_drift --dry-run            # show only
  python manage.py heal_stock_drift --allow-raise --bags 21   # raise for bag #21
"""
import logging

from django.core.management.base import BaseCommand

from catalog.stock_audit import scan_bag_drift, heal_bag_drift, split_by_direction

log = logging.getLogger('catalog')


class Command(BaseCommand):
    help = ('Heals the bag↔stock drift (own packagings). Only DOWN by default; '
            'raising stock needs --allow-raise.')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Change nothing, only show the drift found.')
        parser.add_argument('--allow-raise', action='store_true',
                            help='Allow RAISING stock to the bag value (only after verifying the bag weight!).')
        parser.add_argument('--bags', default='',
                            help='Restrict to specific bags: --bags 21,19')
        parser.add_argument('--limit', type=int, default=25,
                            help='How many rows to show in the report (default 25).')

    def _show(self, title, rows, limit):
        if not rows:
            return
        self.stdout.write(title)
        for v, exp, act, bag in rows[:limit]:
            self.stdout.write(
                f'  v#{v.pk} [{v.sku or "—"}] {(v.name or "")[:45]!r}: '
                f'stock={act} → {exp} (bag#{bag.pk} {bag.quantity}{bag.unit}, '
                f'unit_size={v.unit_size})')
        if len(rows) > limit:
            self.stdout.write(f'  … and {len(rows) - limit} more')

    def handle(self, *args, **opts):
        limit = opts['limit']
        drift, _no_bag, checked = scan_bag_drift()

        only = {int(x) for x in opts['bags'].replace(' ', '').split(',') if x.isdigit()}
        if only:
            drift = [row for row in drift if row[3].pk in only]
            self.stdout.write(f'Limited to bags: {sorted(only)}')

        if not drift:
            self.stdout.write(self.style.SUCCESS(
                f'✓ No drift ({checked} bag-based variants checked)'))
            return

        down, up = split_by_direction(drift)
        self._show(self.style.WARNING('DOWN (storefront overstated — oversell risk):'), down, limit)
        self._show(self.style.WARNING('UP (product not put on sale):'), up, limit)

        if opts['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'DRY-RUN: found {len(drift)} (down {len(down)}, up {len(up)}), nothing changed'))
            return

        # ERROR, not INFO: the appearance of drift = a bug in some bag write path.
        # Healing removes the symptom for the customer, but the trace must remain visible.
        log.error('stock drift: %s own packagings diverged from their bag (down %s, up %s); examples: %s',
                  len(drift), len(down), len(up),
                  ', '.join(f'v#{v.pk}({a}→{e})' for v, e, a, _b in drift[:10]))

        to_heal = down + (up if opts['allow_raise'] else [])
        fixed = heal_bag_drift(to_heal)
        self.stdout.write(self.style.SUCCESS(
            f'✓ Healed {fixed} of {len(to_heal)} ({checked} checked)'))

        if up and not opts['allow_raise']:
            self.stdout.write(self.style.ERROR(
                f'⚠ {len(up)} variants were NOT raised automatically — raising puts the product on '
                f'sale, so the bag weight must be confirmed first. To raise deliberately: '
                f'--allow-raise --bags {",".join(str(b) for b in sorted({r[3].pk for r in up}))}'))
            log.error('stock drift: %s variants need their stock RAISED — not raised '
                      'automatically, waiting for the bag weights to be verified: %s',
                      len(up), sorted({r[3].pk for r in up}))
