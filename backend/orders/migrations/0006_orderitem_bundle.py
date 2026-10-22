from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0022_productvariant_labels'),
        ('orders', '0005_add_invoice_original_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='bundle',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='order_items',
                to='catalog.bundle',
                verbose_name='Bundle',
            ),
        ),
        migrations.AlterField(
            model_name='orderitem',
            name='product_variant',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to='catalog.productvariant',
                verbose_name='Product variant',
            ),
        ),
    ]
