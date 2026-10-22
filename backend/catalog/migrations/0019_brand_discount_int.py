from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0018_brandimage'),
    ]

    operations = [
        migrations.AlterField(
            model_name='brand',
            name='discount_percent',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name='Discount %',
                validators=[django.core.validators.MaxValueValidator(100)],
            ),
        ),
    ]
