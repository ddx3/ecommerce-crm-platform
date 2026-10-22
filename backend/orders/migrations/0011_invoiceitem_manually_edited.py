from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0010_invoice_reverted_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoiceitem',
            name='manually_edited',
            field=models.BooleanField(default=False, verbose_name='Edited manually'),
        ),
    ]
