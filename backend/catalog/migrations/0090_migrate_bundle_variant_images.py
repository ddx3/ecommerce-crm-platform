# Move packaging photos (BundleVariantImage) into the unified BundleImage(variant=...) model,
# so that the bundle photo logic becomes the same as for products.

from django.db import migrations


def forward(apps, schema_editor):
    BundleVariantImage = apps.get_model('catalog', 'BundleVariantImage')
    BundleImage = apps.get_model('catalog', 'BundleImage')
    for bvi in BundleVariantImage.objects.select_related('bundle_variant').all():
        bv = bvi.bundle_variant
        if not bv or not getattr(bv, 'bundle_id', None):
            continue
        BundleImage.objects.create(
            bundle_id=bv.bundle_id,
            variant_id=bv.id,
            image=(bvi.image.name if bvi.image else ''),
            sort_order=bvi.sort_order or 0,
            is_main=False,
            is_hidden=False,
        )


def backward(apps, schema_editor):
    # Irreversible: the photos already live in BundleImage. We delete only the migrated ones
    # (variant != NULL), so as not to touch the bundle's general photos.
    BundleImage = apps.get_model('catalog', 'BundleImage')
    BundleImage.objects.filter(variant__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0089_bundleimage_is_hidden_bundleimage_variant_and_more'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
