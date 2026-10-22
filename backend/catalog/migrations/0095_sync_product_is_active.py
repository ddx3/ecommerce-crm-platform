from django.db import migrations


def resync(apps, schema_editor):
    """Bring Product.is_active in line with the new rule:
    product is active ⟺ it has at least one active NON-fallback variant."""
    Product = apps.get_model('catalog', 'Product')
    ProductVariant = apps.get_model('catalog', 'ProductVariant')
    for pk in Product.objects.values_list('pk', flat=True):
        active = ProductVariant.objects.filter(
            product_id=pk, is_active=True, is_supplier_fallback=False,
        ).exists()
        Product.objects.filter(pk=pk).exclude(is_active=active).update(is_active=active)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0094_alter_bundlevariant_options_and_more'),
    ]

    operations = [
        migrations.RunPython(resync, noop),
    ]
