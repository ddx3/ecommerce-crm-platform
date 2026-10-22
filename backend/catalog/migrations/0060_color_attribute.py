from django.db import migrations, models


def migrate_variant_colors(apps, schema_editor):
    Attribute = apps.get_model('catalog', 'Attribute')
    AttributeValue = apps.get_model('catalog', 'AttributeValue')
    ProductVariant = apps.get_model('catalog', 'ProductVariant')

    variants_with_color = list(ProductVariant.objects.exclude(color='').exclude(color__isnull=True))
    if not variants_with_color:
        return

    # "Color": the canonical color attribute name stored in the catalog DB;
    # the import commands look the attribute up by this exact name.
    color_attr, _ = Attribute.objects.get_or_create(
        name='Color',
        defaults={'is_color': True},
    )
    if not color_attr.is_color:
        color_attr.is_color = True
        color_attr.save(update_fields=['is_color'])

    av_by_hex = {}
    for v in variants_with_color:
        hex_val = (v.color or '').strip()
        if not hex_val:
            continue
        if hex_val not in av_by_hex:
            av, _ = AttributeValue.objects.get_or_create(
                attribute=color_attr,
                value=hex_val,
                defaults={'color_hex': hex_val},
            )
            if not av.color_hex:
                av.color_hex = hex_val
                av.save(update_fields=['color_hex'])
            av_by_hex[hex_val] = av
        v.attribute_values.add(av_by_hex[hex_val])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0059_dropsync_event'),
    ]

    operations = [
        migrations.AddField(
            model_name='attribute',
            name='is_color',
            field=models.BooleanField(default=False, verbose_name='Color'),
        ),
        migrations.AddField(
            model_name='attributevalue',
            name='color_hex',
            field=models.CharField(blank=True, max_length=7, verbose_name='HEX'),
        ),
        migrations.RunPython(migrate_variant_colors, reverse_noop),
        migrations.RemoveField(
            model_name='productvariant',
            name='color',
        ),
    ]
