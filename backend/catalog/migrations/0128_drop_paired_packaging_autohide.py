"""Automatic hiding of the twin packaging (own ↔ factory) removed.

The logic that hid an identical packaging in another form (`paired_visibility_plan` /
`sync_paired_size_visibility`) is removed: it cut half of the cards from the storefront,
and what remained hung there as "sold out". The field `auto_hidden_by_twin`
is removed along with it — without the automatic logic it means nothing.

Before dropping the field we MUST restore visibility for all packagings that were hidden
by the automatic logic: nobody will turn them back on anymore, and they would stay invisible
forever. Manually hidden packagings (auto_hidden_by_twin=False) are not touched —
a person hid them.
"""
from django.db import migrations, models


def unhide_auto_hidden(apps, schema_editor):
    ProductVariant = apps.get_model('catalog', 'ProductVariant')
    Product = apps.get_model('catalog', 'Product')

    hidden = ProductVariant.objects.filter(auto_hidden_by_twin=True)
    product_ids = set(hidden.values_list('product_id', flat=True))
    hidden.update(is_active=True, auto_hidden_by_twin=False)

    # A product is active if and only if it has at least one active non-fallback
    # variant (sync_product_is_active). Signals do not work in a migration, so
    # we recalculate explicitly — otherwise the product would stay hidden while its packaging is
    # visible.
    for pid in product_ids:
        if pid is None:
            continue
        active = ProductVariant.objects.filter(
            product_id=pid, is_active=True, is_supplier_fallback=False,
        ).exists()
        Product.objects.filter(pk=pid).exclude(is_active=active).update(is_active=active)


def noop_reverse(apps, schema_editor):
    """Only the column is restored on reverse (default False).

    It is impossible to restore which ones the automatic logic hid — that information was
    the field itself. Re-hiding would be done by the automatic logic, which no longer exists.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0127_alter_productvariant_stock'),
    ]

    operations = [
        migrations.RunPython(unhide_auto_hidden, noop_reverse),
        migrations.RemoveField(
            model_name='productvariant',
            name='auto_hidden_by_twin',
        ),
    ]
