from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0010_remove_attributevalue_color_add_variant_color'),
    ]

    operations = [
        migrations.AddField(
            model_name='productvariant',
            name='discount_percent',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name='Discount %'),
        ),
        migrations.AddField(
            model_name='productvariant',
            name='discount_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Discount, UAH'),
        ),
    ]
