from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0007_invoicephoto'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='confirmed_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Confirmation date'),
        ),
    ]
