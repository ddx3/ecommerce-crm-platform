"""
Guarantees a single color attribute "Color" (this is the name stored in the DB):
1. Merges all Attributes with is_color=True (and those whose name contains "фурнітур" -
   the stem of the Ukrainian word for hardware/fittings, as in "Hardware color")
   into the single canonical "Color". Values of the duplicates are moved (on a value
   collision — variant.attribute_values is re-linked to the existing AttributeValue).
2. Makes Attribute.name unique.
3. Adds a partial-unique constraint: only one Attribute may have is_color=True.
"""
from django.db import migrations, models


def merge_color_attributes(apps, schema_editor):
    Attribute = apps.get_model('catalog', 'Attribute')
    AttributeValue = apps.get_model('catalog', 'AttributeValue')
    ProductVariant = apps.get_model('catalog', 'ProductVariant')

    # LANGUAGE DATA: the 'фурнітур' stem below ("hardware/fittings") and the legacy name 'Колір'
    # ("Color") match attribute names that older, Ukrainian-language catalog databases may contain.
    canonical = (
        Attribute.objects.filter(name='Color').first()
        or Attribute.objects.filter(name='Колір').first()
        or Attribute.objects.filter(is_color=True).order_by('pk').first()
    )
    if canonical is None:
        return

    if canonical.name != 'Color':
        canonical.name = 'Color'
    if not canonical.is_color:
        canonical.is_color = True
    canonical.save()

    duplicates = (
        Attribute.objects
        .filter(models.Q(is_color=True) | models.Q(name__icontains='фурнітур'))
        .exclude(pk=canonical.pk)
    )

    for dup in duplicates:
        for av in list(dup.values.all()):
            existing = canonical.values.filter(value=av.value).first()
            if existing is not None:
                if not existing.color_hex and av.color_hex:
                    existing.color_hex = av.color_hex
                    existing.save(update_fields=['color_hex'])
                for variant in ProductVariant.objects.filter(attribute_values=av):
                    variant.attribute_values.add(existing)
                    variant.attribute_values.remove(av)
                av.delete()
            else:
                av.attribute = canonical
                av.save(update_fields=['attribute'])
        dup.delete()


def dedupe_by_name(apps, schema_editor):
    """Safety net: if the DB has two Attributes with the same name — merge them."""
    Attribute = apps.get_model('catalog', 'Attribute')
    ProductVariant = apps.get_model('catalog', 'ProductVariant')

    seen = {}
    for attr in Attribute.objects.order_by('pk'):
        key = attr.name.strip().lower()
        if key in seen:
            keeper = seen[key]
            for av in list(attr.values.all()):
                existing = keeper.values.filter(value=av.value).first()
                if existing is not None:
                    if not existing.color_hex and av.color_hex:
                        existing.color_hex = av.color_hex
                        existing.save(update_fields=['color_hex'])
                    for variant in ProductVariant.objects.filter(attribute_values=av):
                        variant.attribute_values.add(existing)
                        variant.attribute_values.remove(av)
                    av.delete()
                else:
                    av.attribute = keeper
                    av.save(update_fields=['attribute'])
            attr.delete()
        else:
            seen[key] = attr


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    # AlterField does not get along with RunPython in one transaction on Postgres
    # (deferred-trigger events), so we disable atomic.
    atomic = False

    dependencies = [
        ('catalog', '0068_alter_bundle_sku'),
    ]

    operations = [
        migrations.RunPython(merge_color_attributes, reverse_noop),
        migrations.RunPython(dedupe_by_name, reverse_noop),
        migrations.AlterField(
            model_name='attribute',
            name='name',
            field=models.CharField(max_length=100, unique=True, verbose_name='Name'),
        ),
        migrations.AddConstraint(
            model_name='attribute',
            constraint=models.UniqueConstraint(
                fields=['is_color'],
                condition=models.Q(is_color=True),
                name='catalog_attribute_single_color',
            ),
        ),
    ]
