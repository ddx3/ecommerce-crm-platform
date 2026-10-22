# -*- coding: utf-8 -*-
"""Test data for the bundle instructions feature: instructions + orders.

FOR LOCAL DEVELOPMENT ONLY. Creates what shows the feature in full:
instructions of different lengths, orders with several bundles, with several
copies of one bundle, with a per-order changed composition and with a bundle
WITHOUT an instruction (to see the placeholder).

The instruction texts are NOT INVENTED: they are assembled from the real `application_norms`
of the bundle, so all doses are genuine. These are stubs for checking the mechanics, not
finished sheets — they must be replaced with real ones before prod.

    python manage.py make_instruction_testdata            # report, writes nothing
    python manage.py make_instruction_testdata --apply
    python manage.py make_instruction_testdata --clean    # remove what was created

Cleanup finds orders by a marker in `source`, so real orders
are not touched. Stock is returned the standard way (deleting an item → _restore).
"""
import re

from django.core.management.base import BaseCommand
from django.db import transaction

MARKER = 'TEST-INSTRUCTIONS'

# Bundles for the test instructions — Bundle.pk (after the BundleVariant→Bundle merge these ids no
# longer match the old BundleVariant.pk; pick current ones by hand if you need to cover specific
# cases).
WITH_INSTRUCTION = [158, 72, 127, 103]
# A bundle deliberately given NO instruction — to check the placeholder in print.
WITHOUT_INSTRUCTION = 110


def _strip(html):
    t = re.sub(r'<[^>]+>', ' ', html or '')
    for a, b in [('&nbsp;', ' '), ('&hellip;', '…'), ('&ndash;', '–'),
                 ('&mdash;', '—'), ('&rsquo;', '’'), ('&deg;', '°'), ('&amp;', '&')]:
        t = t.replace(a, b)
    return re.sub(r'[ \t]+', ' ', t).strip()


def build_instruction(bundle):
    """Build an instruction from the bundle's existing fields. Doses are carried over verbatim."""
    norms = (bundle.application_norms or '').strip()
    if not norms:
        return ''
    title = (bundle.name or '').strip()
    parts = [f'<h1>{title.upper()}</h1>']

    # First paragraph of the description — a short introduction.
    intro = _strip(bundle.description).split('.')
    if intro and len(intro[0]) > 30:
        parts.append(f'<p>{intro[0].strip()}.</p>')

    parts.append('<h3>Application scheme</h3>')
    # application_norms is already HTML — take it as is so as not to lose a single dose.
    parts.append(norms)

    parts.append('<h3>General rules</h3>')
    parts.append('<ul>'
                 '<li>Spray in calm weather, in the morning or in the evening.</li>'
                 '<li>Prepare the solution right before treatment and use it the same day.</li>'
                 '<li>Wear personal protective equipment.</li>'
                 '</ul>')
    parts.append('<p><u>Store in a dry place, out of reach of children and animals.</u></p>')
    return '\n'.join(parts)


class Command(BaseCommand):
    help = 'Test instructions and orders for checking the feature (local only)'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='write to the DB')
        parser.add_argument('--clean', action='store_true', help='remove what was created')

    def handle(self, *args, **opts):
        if opts['clean']:
            return self._clean(opts['apply'])

        apply = opts['apply']
        self.stdout.write(self.style.WARNING(
            'REPORT MODE — nothing written. Add --apply.' if not apply
            else 'WRITING TO THE DB'))

        with transaction.atomic():
            self._instructions(apply)
            self._orders(apply)
            if not apply:
                transaction.set_rollback(True)

    # ---------------------------------------------------------------- instructions
    def _instructions(self, apply):
        from catalog.models import Bundle

        self.stdout.write('\n=== INSTRUCTIONS ===')
        for pk in WITH_INSTRUCTION:
            b = Bundle.objects.filter(pk=pk).first()
            if b is None:
                self.stdout.write(f'  bundle{pk}: NO such bundle — skipped')
                continue
            if (b.instruction_html or '').strip():
                self.stdout.write(f'  bundle{pk}: instruction already exists — leaving it alone')
                continue
            html = build_instruction(b)
            if not html:
                self.stdout.write(f'  bundle{pk}: empty application_norms — skipped')
                continue
            plain = _strip(html)
            self.stdout.write(f'  bundle{pk}: {len(plain)} chars  "{(b.name or "")[:45]}"')
            if apply:
                b.instruction_html = html
                b.save()

        b = Bundle.objects.filter(pk=WITHOUT_INSTRUCTION).first()
        if b:
            self.stdout.write(f'  bundle{WITHOUT_INSTRUCTION}: deliberately WITHOUT an instruction '
                              f'(placeholder check) — "{(b.name or "")[:40]}"')

    # ---------------------------------------------------------------- orders
    def _orders(self, apply):
        from catalog.models import Bundle, ProductVariant
        from orders.models import Order, OrderItem, apply_bundle_component_edits

        self.stdout.write('\n=== ORDERS ===')
        bvs = {pk: Bundle.objects.filter(pk=pk).first()
               for pk in WITH_INSTRUCTION + [WITHOUT_INSTRUCTION]}
        bvs = {k: v for k, v in bvs.items() if v}
        if len(bvs) < 3:
            self.stdout.write(self.style.ERROR('  too few bundles — not creating orders'))
            return

        keys = list(bvs)
        plan = [
            ('One bundle', [(keys[0], 1)]),
            ('Two different bundles', [(keys[0], 1), (keys[1], 1)]),
            ('One bundle ×3 (check of sheet copies)', [(keys[1], 3)]),
            ('Bundle + a regular product', [(keys[2], 1)]),
            ('Bundle with a CHANGED composition ("needs review" queue)', [(keys[0], 1)]),
            ('Bundle WITHOUT an instruction (placeholder)', [(WITHOUT_INSTRUCTION, 1)]),
        ]

        names = ['Jane Doe', 'John Roe', 'Mary Major',
                 'Richard Miles', 'Alex Sample', 'Sam Example']

        for i, (title, lines) in enumerate(plan):
            desc = ', '.join(f'bundle{pk}×{q}' for pk, q in lines if pk in bvs)
            self.stdout.write(f'  {i + 1}. {title}: {desc}')
            if not apply:
                continue

            order = Order.objects.create(
                customer_name=f'TEST {names[i]}',
                customer_phone=f'06700000{i:02d}',
                customer_email=f'test{i}@example.com',
                delivery_type='nova_poshta',
                delivery_address=f'Kyiv, branch No. {i + 1}',
                payment_method='cod',          # cash on delivery: stock is deducted at creation
                payment_status='pending',
                status='new',
                source=MARKER,
                manager_comment=f'{MARKER}: {title}',
            )
            for pk, qty in lines:
                b = bvs.get(pk)
                if not b:
                    continue
                OrderItem.objects.create(
                    order=order, bundle=b,
                    quantity=qty, price=b.get_price() or 0,
                )
            # A bundle + a regular product
            if i == 3:
                pv = (ProductVariant.objects
                      .filter(is_active=True, is_dropshipping=False, stock__gt=5)
                      .exclude(is_supplier_fallback=True).first())
                if pv:
                    OrderItem.objects.create(order=order, product_variant=pv,
                                             quantity=1, price=pv.price or 0)
            # Per-order component replacement
            if i == 4:
                item = order.items.filter(bundle__isnull=False).first()
                comps = list(item.bundle_components.all())
                if comps:
                    swap_to = (ProductVariant.objects
                               .filter(is_active=True, is_dropshipping=False, stock__gt=5)
                               .exclude(pk__in=[c.product_variant_id for c in comps])
                               .exclude(is_supplier_fallback=True).first())
                    if swap_to:
                        payload = [{'comp_id': c.pk,
                                    'variant_id': swap_to.pk if c is comps[0] else c.product_variant_id,
                                    'qty': c.quantity} for c in comps]
                        apply_bundle_component_edits(item, payload, order)
                        self.stdout.write(f'       replaced "{comps[0].product_variant_name}" '
                                          f'→ "{swap_to.name}"')
            order.refresh_from_db()
            self.stdout.write(f'       created No. {order.order_number} (id={order.pk})')

    # ---------------------------------------------------------------- cleanup
    def _clean(self, apply):
        from catalog.models import Bundle, BundleInstructionOverride
        from orders.models import Order

        orders = Order.objects.filter(source=MARKER)
        self.stdout.write(f'Test orders: {orders.count()}')
        for o in orders:
            self.stdout.write(f'  No. {o.order_number} — {o.customer_name}')
        bundles = Bundle.objects.filter(pk__in=WITH_INSTRUCTION).exclude(instruction_html='')
        self.stdout.write(f'Test instructions: {bundles.count()} '
                          f'(bundle{list(bundles.values_list("pk", flat=True))})')

        if not apply:
            self.stdout.write(self.style.WARNING('Add --apply to remove them.'))
            return
        with transaction.atomic():
            for o in orders:
                # Delete the items one by one — post_delete will return the stock.
                for it in o.items.all():
                    it.delete()
                o.delete()
            BundleInstructionOverride.objects.filter(
                bundle__pk__in=WITH_INSTRUCTION).delete()
            bundles.update(instruction_html='', instruction_map={}, instruction_stamp='')
        self.stdout.write(self.style.SUCCESS('Removed.'))
