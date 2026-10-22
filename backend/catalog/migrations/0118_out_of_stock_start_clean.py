"""Starting from a clean slate for the "Sold out" section.

At launch a couple of hundred packagings already sit at zero — they ran out long ago
and are of no interest as an event. We set a date for them (so the bookkeeping is
consistent) and immediately dismiss them via NotificationState, so the inbox starts
empty and shows only what runs out from now on.

Dismissal is tied to the date: NotificationState.updated_at is set AFTER
out_of_stock_at, and the section shows a row only if it ran out later than its
dismissal. So when such a packaging is restocked and then goes to zero again —
it surfaces normally as a new event.
"""
from django.db import migrations
from django.utils import timezone


def start_clean(apps, schema_editor):
    ProductVariant = apps.get_model('catalog', 'ProductVariant')
    NotificationState = apps.get_model('catalog', 'NotificationState')

    now = timezone.now()
    stale = list(
        ProductVariant.objects
        .filter(stock=0, is_dropshipping=False, is_hybrid=False, is_supplier_fallback=False)
        .values_list('id', flat=True)
    )
    if not stale:
        return
    ProductVariant.objects.filter(id__in=stale).update(out_of_stock_at=now)
    existing = set(
        NotificationState.objects
        .filter(section='stock_out', object_id__in=stale)
        .values_list('object_id', flat=True)
    )
    NotificationState.objects.bulk_create(
        [NotificationState(section='stock_out', object_id=vid, step=2)
         for vid in stale if vid not in existing],
        batch_size=500,
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [('catalog', '0117_out_of_stock_mark')]
    operations = [migrations.RunPython(start_clean, noop)]
