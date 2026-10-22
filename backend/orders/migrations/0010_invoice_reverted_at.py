from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0009_invoicenamemapping'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='reverted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Revert date'),
        ),
    ]
