from django.db import migrations, models


def backfill_baseline(apps, schema_editor):
    """For already open purchases, take the stock level from the latest snapshot of the card they were
marked from."""
    PlannedPurchase = apps.get_model('analyst', 'PlannedPurchase')
    for po in PlannedPurchase.objects.filter(status='open', stock_at_order__isnull=True).select_related('finding'):
        item = ((po.finding.data or {}).get('item') or {}) if po.finding_id else {}
        stock = item.get('stock')
        if isinstance(stock, (int, float)) and not isinstance(stock, bool):
            po.stock_at_order = stock
            po.save(update_fields=['stock_at_order'])


class Migration(migrations.Migration):

    dependencies = [
        ('analyst', '0009_codefix_auth_reviewing'),
    ]

    operations = [
        migrations.AddField(
            model_name='plannedpurchase',
            name='stock_at_order',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=14, null=True,
                help_text="The item's stock when the owner pressed ✓: when it rises significantly, the Analyst considers the order received",
                verbose_name='Stock at the moment of ✓'),
        ),
        migrations.RunPython(backfill_baseline, migrations.RunPython.noop),
    ]
