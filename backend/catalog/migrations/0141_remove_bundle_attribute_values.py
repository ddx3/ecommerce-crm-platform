# Bundle attributes removed entirely: bundles have 0 relations (verified on prod), and the
# storefront neither showed them nor filtered by them.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0140_bundleitem_ordering'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='bundle',
            name='attribute_values',
        ),
    ]
