from django.db import migrations


def backfill_snapshots(apps, schema_editor):
    OrderItem = apps.get_model('orders', 'OrderItem')
    for item in OrderItem.objects.filter(product_variant_name='').iterator():
        if item.product_variant_id:
            try:
                pv = item.product_variant
                item.product_variant_name = pv.name or ''
                item.product_variant_sku = pv.sku or ''
            except Exception:
                continue
        elif item.bundle_id:
            bundle = item.bundle
            bv = item.bundle_variant if item.bundle_variant_id else None
            bv_name = bv.name if bv else ''
            item.product_variant_name = f'{bundle.name} ({bv_name})' if bv_name else bundle.name
            item.product_variant_sku = (bv.sku if bv else None) or bundle.sku or ''
        else:
            continue
        item.save(update_fields=['product_variant_name', 'product_variant_sku'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0020_orderitem_product_variant_name_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_snapshots, noop_reverse),
    ]
