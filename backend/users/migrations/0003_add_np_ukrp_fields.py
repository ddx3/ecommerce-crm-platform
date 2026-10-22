from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_add_delivery_fields'),
    ]

    operations = [
        migrations.RemoveField(model_name='user', name='delivery_city'),
        migrations.RemoveField(model_name='user', name='delivery_address'),
        migrations.RemoveField(model_name='user', name='delivery_postal'),
        migrations.AddField(
            model_name='user',
            name='np_city',
            field=models.CharField(blank=True, max_length=100, verbose_name='Nova Poshta: city'),
        ),
        migrations.AddField(
            model_name='user',
            name='np_city_ref',
            field=models.CharField(blank=True, max_length=100, verbose_name='Nova Poshta: city ref'),
        ),
        migrations.AddField(
            model_name='user',
            name='np_warehouse',
            field=models.CharField(blank=True, max_length=255, verbose_name='Nova Poshta: branch'),
        ),
        migrations.AddField(
            model_name='user',
            name='ukrp_address',
            field=models.CharField(blank=True, max_length=255, verbose_name='Ukrposhta: address'),
        ),
    ]
