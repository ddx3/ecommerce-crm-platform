"""Merge BundleVariant → Bundle, step 1/4: add the new fields and relations (nothing is removed).

A bundle no longer has 'variants': 1 Bundle = 1 product on the storefront. All fields that
lived on BundleVariant (SKU, price, discounts, instruction, labels, attributes…) move to
Bundle; child tables (composition, photos, subscriptions, favorites, instruction edits) get
an FK to Bundle alongside the old FK to the variant.

The step is purely additive: the old code and old tables stay intact, the new columns are
empty until the next steps (0137 — archive, 0138 — copy, 0139 — drop the old).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0135_low_stock_threshold_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='bundle',
            name='price',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Price'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='stock',
            field=models.PositiveIntegerField(null=True, blank=True, verbose_name='Stock'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='discount_percent',
            field=models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name='Discount %'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='discount_amount',
            field=models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, verbose_name='Discount, UAH'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='application_norms',
            field=models.TextField(blank=True, default='', verbose_name='Usage / Norms'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='instruction_html',
            field=models.TextField(
                blank=True, default='', verbose_name='Instructions (print)',
                help_text='Printed instructions placed in the box. The same text is opened '
                          'to the customer in "My orders". It does NOT go to the public "Usage" tab.',
            ),
        ),
        migrations.AddField(
            model_name='bundle',
            name='instruction_map',
            field=models.JSONField(default=dict, blank=True, editable=False, verbose_name='Instructions markup'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='instruction_stamp',
            field=models.CharField(
                max_length=40, blank=True, default='', editable=False,
                verbose_name='Instructions fingerprint',
                help_text='sha1 of the body. If it changes, all corrections made for a changed composition become stale.',
            ),
        ),
        migrations.AddField(
            model_name='bundle',
            name='instruction_updated_at',
            field=models.DateTimeField(null=True, blank=True, editable=False, verbose_name='Instructions updated'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='attribute_values',
            field=models.ManyToManyField(blank=True, related_name='bundles', to='catalog.attributevalue', verbose_name='Attributes'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='labels',
            field=models.ManyToManyField(blank=True, related_name='bundles', to='catalog.label', verbose_name='Labels'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='sort_order',
            field=models.PositiveIntegerField(default=0, verbose_name='Order'),
        ),
        migrations.AlterField(
            model_name='bundle',
            name='sku',
            field=models.CharField(max_length=10, unique=True, blank=True, null=True, verbose_name='SKU'),
        ),
        migrations.AddField(
            model_name='bundleitem',
            name='bundle',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='items_new', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AddField(
            model_name='bundleinstructionoverride',
            name='bundle',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='instruction_overrides_new', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AddField(
            model_name='stocknotification',
            name='bundle',
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='stock_notifications_new', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AddField(
            model_name='favorite',
            name='bundle',
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='favorites_new', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
    ]
