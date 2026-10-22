"""
Transition from "variant photo" to "color photo".

1. Adds ProductImage.color_value (FK to AttributeValue, nullable).
2. Fills color_value via variant.attribute_values (is_color=True).
3. Deduplicates: for each (product, color_value, image) keeps ONE record;
   deletes the rest (variant != NULL).
4. Clears variant on photos that got a color_value (the photo now
   belongs to a color, not to a specific variant).
"""
from django.db import migrations, models
from collections import defaultdict


def migrate_to_color_based_images(apps, schema_editor):
    ProductImage = apps.get_model('catalog', 'ProductImage')
    Attribute = apps.get_model('catalog', 'Attribute')

    color_attr_ids = list(Attribute.objects.filter(is_color=True).values_list('pk', flat=True))
    if not color_attr_ids:
        return

    # 1) Fill color_value
    qs = ProductImage.objects.filter(color_value__isnull=True, variant__isnull=False)
    updated = 0
    for img in qs.iterator():
        # first color AttributeValue among the variant's attributes
        color_av = (
            img.variant.attribute_values
                .filter(attribute_id__in=color_attr_ids)
                .order_by('pk')
                .first()
        )
        if color_av:
            img.color_value_id = color_av.pk
            img.variant_id = None  # the photo belongs to the color
            img.save(update_fields=['color_value', 'variant'])
            updated += 1

    # 2) Deduplicate: (product, color_value, image.name) → one record
    groups = defaultdict(list)
    for img in ProductImage.objects.filter(color_value__isnull=False).only(
        'pk', 'product_id', 'color_value_id', 'image', 'is_main', 'sort_order'
    ):
        key = (img.product_id, img.color_value_id, img.image.name)
        groups[key].append(img)

    deleted = 0
    for key, imgs in groups.items():
        if len(imgs) <= 1:
            continue
        imgs.sort(key=lambda x: (not x.is_main, x.sort_order, x.pk))
        keeper = imgs[0]
        for dup in imgs[1:]:
            dup.delete()
            deleted += 1


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0069_merge_color_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='productimage',
            name='color_value',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=models.deletion.SET_NULL,
                related_name='product_images',
                to='catalog.attributevalue',
                verbose_name='Color',
                help_text='The photo belongs to this color — shared by all variants of this color.',
            ),
        ),
        migrations.RunPython(migrate_to_color_based_images, reverse_noop),
    ]
