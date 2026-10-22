"""Merge BundleVariant → Bundle, step 4/4: drop the old and enable the new constraints.

By this step the data is already copied (0138) and archived (0137), and order items are
detached from the variant (orders 0054). Here we delete BundleVariant and the old FKs and
switch `bundle` in the child tables from temporary relations to permanent ones.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0138_bundle_merge_copy_data'),
        ('orders', '0054_remove_orderitem_bundle_variant'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='stocknotification',
            name='stocknotif_exactly_one_target',
        ),
        migrations.RemoveConstraint(
            model_name='bundleinstructionoverride',
            name='bundleinstroverride_uniq_variant_signature',
        ),
        migrations.RemoveConstraint(
            model_name='favorite',
            name='fav_uniq_device_bundle',
        ),
        migrations.AlterUniqueTogether(
            name='stocknotification',
            unique_together=set(),
        ),
        migrations.RemoveField(
            model_name='bundleimage',
            name='variant',
        ),
        migrations.RemoveField(
            model_name='bundleitem',
            name='bundle_variant',
        ),
        migrations.RemoveField(
            model_name='bundleinstructionoverride',
            name='bundle_variant',
        ),
        migrations.RemoveField(
            model_name='stocknotification',
            name='bundle_variant',
        ),
        migrations.RemoveField(
            model_name='favorite',
            name='bundle_variant',
        ),
        migrations.DeleteModel(
            name='BundleVariantImage',
        ),
        migrations.DeleteModel(
            name='BundleVariant',
        ),
        migrations.AlterField(
            model_name='bundleitem',
            name='bundle',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='items', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AlterField(
            model_name='bundleinstructionoverride',
            name='bundle',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='instruction_overrides', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AlterField(
            model_name='stocknotification',
            name='bundle',
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='stock_notifications', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AlterField(
            model_name='favorite',
            name='bundle',
            field=models.ForeignKey(
                null=True, blank=True, on_delete=django.db.models.deletion.CASCADE,
                related_name='favorites', to='catalog.bundle', verbose_name='Bundle',
            ),
        ),
        migrations.AlterUniqueTogether(
            name='stocknotification',
            unique_together={('email', 'variant'), ('email', 'bundle')},
        ),
        migrations.AddConstraint(
            model_name='stocknotification',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(('bundle__isnull', True), ('variant__isnull', False)),
                    models.Q(('bundle__isnull', False), ('variant__isnull', True)),
                    _connector='OR',
                ),
                name='stocknotif_exactly_one_target',
            ),
        ),
        migrations.AddConstraint(
            model_name='bundleinstructionoverride',
            constraint=models.UniqueConstraint(
                fields=('bundle', 'signature'),
                name='bundleinstroverride_uniq_bundle_signature',
            ),
        ),
        migrations.AddConstraint(
            model_name='favorite',
            constraint=models.UniqueConstraint(
                condition=models.Q(('bundle__isnull', False)),
                fields=('device_id', 'bundle'),
                name='fav_uniq_device_bundle',
            ),
        ),
    ]
