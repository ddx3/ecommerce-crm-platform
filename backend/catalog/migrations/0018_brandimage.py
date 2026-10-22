from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0017_alter_sku_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='BrandImage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('image', models.ImageField(upload_to='brands/', verbose_name='Image')),
                ('sort_order', models.IntegerField(default=0)),
                ('temp_key', models.CharField(blank=True, db_index=True, max_length=100)),
                ('brand', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='images', to='catalog.brand', verbose_name='Brand')),
            ],
            options={
                'verbose_name': 'Brand photo',
                'verbose_name_plural': 'Brand photos',
                'ordering': ['sort_order', 'pk'],
            },
        ),
    ]
