from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0012_bundle_name_instead_of_product'),
    ]

    operations = [
        # Remove old fields
        migrations.RemoveField(model_name='bundle', name='custom_price'),
        migrations.RemoveField(model_name='bundle', name='custom_stock'),

        # Add new fields to Bundle
        migrations.AddField(
            model_name='bundle',
            name='description',
            field=models.TextField(blank=True, verbose_name='Description'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='sku',
            field=models.CharField(blank=True, max_length=100, verbose_name='SKU'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Price'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='old_price',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Old price'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='discount_percent',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name='Discount %'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='discount_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True, verbose_name='Discount, UAH'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='stock',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='Stock'),
        ),
        migrations.AddField(
            model_name='bundle',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='bundle',
            name='categories',
            field=models.ManyToManyField(blank=True, related_name='bundles', to='catalog.category', verbose_name='Categories'),
        ),
        # Add unique constraint to sku
        migrations.AlterField(
            model_name='bundle',
            name='sku',
            field=models.CharField(blank=True, max_length=100, unique=True, verbose_name='SKU'),
        ),

        # Create BundleImage model
        migrations.CreateModel(
            name='BundleImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='bundles/', verbose_name='Image')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='Order')),
                ('is_main', models.BooleanField(default=False, verbose_name='Main photo')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('bundle', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='images',
                    to='catalog.bundle',
                    verbose_name='Bundle',
                )),
            ],
            options={
                'verbose_name': 'Bundle photo',
                'verbose_name_plural': 'Bundle photos',
                'ordering': ['sort_order'],
            },
        ),
    ]
