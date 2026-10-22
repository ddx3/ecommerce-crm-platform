from django.db import migrations
from django.contrib.postgres.operations import TrigramExtension


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0038_label_auto_days'),
    ]

    operations = [
        TrigramExtension(),
    ]
