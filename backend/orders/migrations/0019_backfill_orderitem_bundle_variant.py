from django.db import migrations


def backfill_bundle_variant(apps, schema_editor):
    OrderItem = apps.get_model('orders', 'OrderItem')
    BundleVariant = apps.get_model('catalog', 'BundleVariant')
    # Cache the first active (or just the first) packaging of each bundle
    cache = {}

    def first_variant_for(bundle_id):
        if bundle_id in cache:
            return cache[bundle_id]
        bv = BundleVariant.objects.filter(
            bundle_id=bundle_id, is_active=True
        ).order_by('sort_order', 'pk').first()
        if bv is None:
            bv = BundleVariant.objects.filter(
                bundle_id=bundle_id
            ).order_by('sort_order', 'pk').first()
        cache[bundle_id] = bv
        return bv

    qs = OrderItem.objects.filter(bundle__isnull=False, bundle_variant__isnull=True)
    for item in qs.iterator():
        bv = first_variant_for(item.bundle_id)
        if bv is not None:
            item.bundle_variant = bv
            item.save(update_fields=['bundle_variant'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0018_orderitem_bundle_variant'),
        ('catalog', '0062_remove_bundle_discount_amount_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_bundle_variant, noop_reverse),
    ]
