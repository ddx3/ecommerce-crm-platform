from django.db import migrations

SYSTEM_LABELS = [
    {'slug': 'novynka',         'name': 'New',        'color': '#22c55e'},
    {'slug': 'pid-zamovlennya', 'name': 'Made to order', 'color': '#f59e0b'},
    {'slug': 'aktsia',          'name': 'Sale',           'color': '#ef4444'},
    {'slug': 'khit',            'name': 'Hit',             'color': '#8b5cf6'},
]


def create_system_labels(apps, schema_editor):
    Label = apps.get_model('catalog', 'Label')
    for data in SYSTEM_LABELS:
        Label.objects.get_or_create(
            slug=data['slug'],
            defaults={'name': data['name'], 'color': data['color'], 'is_system': True},
        )


def delete_system_labels(apps, schema_editor):
    Label = apps.get_model('catalog', 'Label')
    Label.objects.filter(slug__in=[d['slug'] for d in SYSTEM_LABELS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0036_label_system_variantlabel'),
    ]

    operations = [
        migrations.RunPython(create_system_labels, delete_system_labels),
    ]
