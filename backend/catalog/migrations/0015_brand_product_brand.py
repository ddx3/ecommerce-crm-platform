from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0014_bundleimage_temp_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='Brand',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200, verbose_name='Name')),
                ('slug', models.SlugField(blank=True, max_length=200, unique=True, verbose_name='Slug')),
                ('logo', models.ImageField(blank=True, null=True, upload_to='brands/', verbose_name='Logo')),
                ('description', models.TextField(blank=True, verbose_name='Description')),
                ('discount_percent', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True, verbose_name='Discount %')),
                ('is_active', models.BooleanField(default=True, verbose_name='Visible')),
                ('meta_title', models.CharField(blank=True, max_length=200, verbose_name='Meta Title')),
                ('meta_description', models.TextField(blank=True, verbose_name='Meta Description')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Brand',
                'verbose_name_plural': 'Brands',
                'ordering': ['name'],
            },
        ),
        migrations.AddField(
            model_name='product',
            name='brand',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='products',
                to='catalog.brand',
                verbose_name='Brand',
            ),
        ),
    ]
