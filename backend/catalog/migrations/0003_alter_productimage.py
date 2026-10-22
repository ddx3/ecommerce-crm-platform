from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0002_alter_product_is_active_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='productimage',
            name='variant',
        ),
        migrations.AddField(
            model_name='productimage',
            name='product',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='images',
                to='catalog.product',
                verbose_name='Product',
            ),
        ),
    ]
