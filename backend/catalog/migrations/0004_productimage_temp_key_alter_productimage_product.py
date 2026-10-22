from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_alter_productimage'),
    ]

    operations = [
        migrations.AddField(
            model_name='productimage',
            name='temp_key',
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name='productimage',
            name='product',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='images',
                to='catalog.product',
                verbose_name='Product',
            ),
        ),
    ]
