from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0044_add_variant_to_review'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockNotification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(verbose_name='Email')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('is_notified', models.BooleanField(default=False, verbose_name='Notified')),
                ('variant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='catalog.productvariant', verbose_name='Variant')),
            ],
            options={
                'verbose_name': 'Back-in-stock subscription',
                'verbose_name_plural': 'Back-in-stock subscriptions',
                'unique_together': {('email', 'variant')},
            },
        ),
    ]
