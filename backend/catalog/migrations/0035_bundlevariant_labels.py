from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0034_bundlevariant_attributes'),
    ]

    operations = [
        migrations.AddField(
            model_name='bundlevariant',
            name='labels',
            field=models.ManyToManyField(blank=True, related_name='bundle_variants', to='catalog.label', verbose_name='Labels'),
        ),
    ]
