from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0082_review_is_rejected'),
    ]

    operations = [
        migrations.AddField(
            model_name='productvariant',
            name='stock_manual',
            field=models.BooleanField(
                default=False,
                verbose_name='Manual stock',
                help_text='If True, variant.stock is set manually and is NOT recalculated from the bag (for own packagings).',
            ),
        ),
    ]
