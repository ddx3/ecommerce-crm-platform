from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0040_merge_0039_enable_pg_trgm_0039_fix_stock_auto_default'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;',
            reverse_sql='DROP EXTENSION IF EXISTS fuzzystrmatch;',
        ),
    ]
