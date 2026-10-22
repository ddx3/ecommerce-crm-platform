from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0030_migrate_bundleitems_to_bundlevariant'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bundleitem',
            name='bundle_variant',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='items',
                to='catalog.bundlevariant',
                verbose_name='Bundle packaging',
            ),
        ),
        migrations.RemoveField(
            model_name='bundleitem',
            name='bundle',
        ),
    ]
