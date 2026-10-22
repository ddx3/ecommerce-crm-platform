from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0013_bundle_add_fields'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bundleimage',
            name='bundle',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='images',
                to='catalog.bundle',
                verbose_name='Bundle',
            ),
        ),
        migrations.AddField(
            model_name='bundleimage',
            name='temp_key',
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
    ]
