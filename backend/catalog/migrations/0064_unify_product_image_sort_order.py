from django.db import migrations


def forwards(apps, schema_editor):
    """Renumber sort_order for all ProductImage of each product into a single
    sequence. Order within a product:
        1) variant photos (variant != NULL) — first by existing sort_order, then pk;
        2) general product photos (variant == NULL) — first by existing sort_order, then pk.

    Rationale: the packaging comes before the instruction — that is what should be shown on the
    card.
    The admin can then freely reorder via the UI.
    """
    ProductImage = apps.get_model('catalog', 'ProductImage')
    Product = apps.get_model('catalog', 'Product')

    for product_id in Product.objects.values_list('id', flat=True).iterator():
        imgs = list(
            ProductImage.objects.filter(product_id=product_id)
            .order_by('sort_order', 'pk')
            .only('id', 'variant_id', 'sort_order')
        )
        # Stable sorting: variant photos first, then general ones.
        imgs.sort(key=lambda i: (0 if i.variant_id else 1,))
        for idx, img in enumerate(imgs):
            new_order = idx + 1
            if img.sort_order != new_order:
                ProductImage.objects.filter(pk=img.pk).update(sort_order=new_order)


def backwards(apps, schema_editor):
    # There is no reverse transformation — sort_order cannot be restored backwards
    # without a backup. Left as a no-op; no data is lost.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0063_bundlevariantimage_temp_key_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
