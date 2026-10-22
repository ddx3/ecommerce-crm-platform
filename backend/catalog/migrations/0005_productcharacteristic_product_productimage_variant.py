from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_productimage_temp_key_alter_productimage_product'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcharacteristic',
            name='product',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='characteristics',
                to='catalog.product',
                verbose_name='Product',
            ),
        ),
        migrations.AlterField(
            model_name='productcharacteristic',
            name='variant',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='characteristics',
                to='catalog.productvariant',
                verbose_name='Variant',
            ),
        ),
        migrations.AddField(
            model_name='productimage',
            name='variant',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='images',
                to='catalog.productvariant',
                verbose_name='Variant',
            ),
        ),
    ]
