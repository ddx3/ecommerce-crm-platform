from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0028_add_price_stock_auto'),
    ]

    operations = [
        migrations.CreateModel(
            name='BundleVariant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(blank=True, max_length=300, verbose_name='Name')),
                ('sku', models.CharField(blank=True, max_length=10, null=True, unique=True, verbose_name='SKU')),
                ('price', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Price')),
                ('stock', models.PositiveIntegerField(blank=True, null=True, verbose_name='Stock')),
                ('discount_percent', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name='Discount %')),
                ('discount_amount', models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Discount, UAH')),
                ('is_active', models.BooleanField(default=True, verbose_name='Visible')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='Order')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('bundle', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='variants', to='catalog.bundle', verbose_name='Bundle')),
            ],
            options={
                'verbose_name': 'Bundle packaging',
                'verbose_name_plural': 'Bundle packagings',
                'ordering': ['sort_order', 'pk'],
            },
        ),
        migrations.AddField(
            model_name='bundleitem',
            name='bundle_variant',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='items',
                to='catalog.bundlevariant',
                verbose_name='Bundle packaging',
            ),
        ),
    ]
