from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0046_remove_bundle_stock'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductComposition',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='Name')),
                ('value', models.CharField(blank=True, max_length=300, verbose_name='Value')),
                ('sort_order', models.PositiveIntegerField(default=0, verbose_name='Order')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='compositions', to='catalog.product', verbose_name='Product')),
            ],
            options={
                'verbose_name': 'Composition',
                'verbose_name_plural': 'Composition',
                'ordering': ['sort_order'],
            },
        ),
    ]
