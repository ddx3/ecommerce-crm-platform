"""
One-off recalculation of stock-dependent auto labels that may have drifted, because stock was
changed bypassing the post_save signal (order write-off/return does a queryset .update()/F(), which
does not trigger auto_apply_labels).

- Hybrids (50/50): stock==0 → must have the "made to order" label, stock>0 → must
  not (auto).
- --aktsia: additionally removes a false "Sale" label from variants with zero/missing
  discount (discount_percent=0, old_price<=price, etc.) and sets it where the discount is real.

DRY-RUN by default (only shows). Writes changes only with --apply.
"""
from django.core.management.base import BaseCommand

from catalog.models import ProductVariant, VariantLabel, Label
from catalog import signals


class Command(BaseCommand):
    help = 'Recalculate stock-dependent auto labels (made to order / sale). Dry-run without --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Apply the changes (without the flag, only show them).')
        parser.add_argument('--aktsia', action='store_true',
                            help='Additionally recalculate the "Sale" label across all active variants.')

    def handle(self, *args, **opts):
        apply = opts['apply']
        pid = Label.objects.filter(slug=Label.SLUG_PID_ZAMOVLENNYA).first()
        if not pid:
            self.stdout.write(self.style.ERROR('No "made to order" label — skipping the hybrids.'))
        else:
            add_n = rem_n = 0
            hybrids = ProductVariant.objects.filter(is_hybrid=True).exclude(is_dropshipping=True)
            for v in hybrids.iterator():
                has_any = VariantLabel.objects.filter(variant=v, label=pid).exists()
                has_auto = VariantLabel.objects.filter(variant=v, label=pid, is_auto=True).exists()
                if v.stock == 0 and not has_any:
                    add_n += 1
                    if apply:
                        signals._handle_hybrid_pid_zamovlennya(v)
                elif v.stock > 0 and has_auto:
                    rem_n += 1
                    if apply:
                        signals._handle_hybrid_pid_zamovlennya(v)
            verb = 'Added' if apply else 'Need to add'
            verb2 = 'Removed' if apply else 'Need to remove'
            self.stdout.write(self.style.SUCCESS(
                f'Hybrids "made to order": {verb} {add_n}, {verb2} {rem_n} '
                f'(total hybrids: {hybrids.count()}).'))

        if opts['aktsia']:
            aktsia = Label.objects.filter(slug=Label.SLUG_AKTSIA).first()
            if not aktsia:
                self.stdout.write(self.style.ERROR('No "Sale" label — skipping.'))
            else:
                add_n = rem_n = 0
                for v in ProductVariant.objects.filter(is_active=True).iterator():
                    has_any = VariantLabel.objects.filter(variant=v, label=aktsia).exists()
                    has_auto = VariantLabel.objects.filter(variant=v, label=aktsia, is_auto=True).exists()
                    pct = v.discount_percent
                    target = (
                        (pct is not None and 0 < pct < 100)
                        or (v.discount_amount is not None and v.discount_amount > 0)
                        or (v.old_price is not None and v.price is not None and v.old_price > v.price)
                    )
                    if target and not has_any:
                        add_n += 1
                        if apply:
                            signals._handle_aktsia(v)
                    elif not target and has_auto:
                        rem_n += 1
                        if apply:
                            signals._handle_aktsia(v)
                verb = 'Added' if apply else 'Need to add'
                verb2 = 'Removed' if apply else 'Need to remove'
                self.stdout.write(self.style.SUCCESS(
                    f'"Sale": {verb} {add_n}, {verb2} {rem_n}.'))

        if not apply:
            self.stdout.write(self.style.WARNING('DRY-RUN. Run with --apply to write.'))
