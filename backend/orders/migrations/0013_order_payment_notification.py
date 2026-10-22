from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0012_order_payment_screenshot_alter_invoice_status_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='payment_notification',
            field=models.BooleanField(default=False, verbose_name='New payment (notification)'),
        ),
    ]
