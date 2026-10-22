from django.db import migrations


def migrate_bundle_items(apps, schema_editor):
    Bundle = apps.get_model('catalog', 'Bundle')
    BundleVariant = apps.get_model('catalog', 'BundleVariant')
    BundleItem = apps.get_model('catalog', 'BundleItem')

    for bundle in Bundle.objects.all():
        bv = BundleVariant.objects.create(
            bundle=bundle,
            name=bundle.name,
            sku=bundle.sku if bundle.sku else None,
            price=bundle.price,
            stock=bundle.stock,
            discount_percent=bundle.discount_percent,
            discount_amount=bundle.discount_amount,
            is_active=bundle.is_active,
            sort_order=0,
        )
        BundleItem.objects.filter(bundle=bundle).update(bundle_variant=bv)


def reverse_migrate(apps, schema_editor):
    BundleItem = apps.get_model('catalog', 'BundleItem')
    BundleItem.objects.update(bundle_variant=None)


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0029_bundlevariant_bundleitem_bundle_variant'),
    ]

    operations = [
        migrations.RunPython(migrate_bundle_items, reverse_migrate),
    ]
