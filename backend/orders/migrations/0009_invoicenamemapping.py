from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0035_bundlevariant_labels'),
        ('orders', '0008_invoice_confirmed_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='InvoiceNameMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('invoice_name', models.CharField(max_length=500, unique=True, verbose_name='Name from invoice')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('variant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='catalog.productvariant', verbose_name='Product in DB')),
            ],
            options={
                'verbose_name': 'Invoice name mapping',
                'verbose_name_plural': 'Invoice name mappings',
            },
        ),
    ]
