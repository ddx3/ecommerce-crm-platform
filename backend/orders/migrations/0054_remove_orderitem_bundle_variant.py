"""BundleVariant → Bundle merge: remove the variant reference from the order item.

The order item already holds `bundle` (for all items with a variant it matches the
variant's bundle), and the stock composition/names live in the item snapshots. Before
dropping the column we save the values in the `legacy_bundles` archive (the schema is
created by catalog 0137).
"""
from django.db import migrations


def archive_and_fill(apps, schema_editor):
    OrderItem = apps.get_model('orders', 'OrderItem')
    BundleVariant = apps.get_model('catalog', 'BundleVariant')
    # Items that have only a variant (none in prod): pull the bundle in from the variant.
    for it in OrderItem.objects.filter(bundle_variant__isnull=False, bundle__isnull=True):
        bid = BundleVariant.objects.filter(pk=it.bundle_variant_id).values_list('bundle_id', flat=True).first()
        if bid:
            OrderItem.objects.filter(pk=it.pk).update(bundle_id=bid)
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('CREATE SCHEMA IF NOT EXISTS legacy_bundles')
        schema_editor.execute(
            'CREATE TABLE IF NOT EXISTS legacy_bundles.orderitem_bundle_variant AS '
            'SELECT id, bundle_id, bundle_variant_id FROM public.orders_orderitem '
            'WHERE bundle_variant_id IS NOT NULL')


def noop_reverse(apps, schema_editor):
    """Keep the archive."""


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0053_order_payment_method_cash_own_expense'),
        ('catalog', '0138_bundle_merge_copy_data'),
    ]

    operations = [
        migrations.RunPython(archive_and_fill, noop_reverse),
        migrations.RemoveField(
            model_name='orderitem',
            name='bundle_variant',
        ),
    ]
