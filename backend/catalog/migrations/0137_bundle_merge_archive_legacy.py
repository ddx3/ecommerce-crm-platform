"""Merge BundleVariant → Bundle, step 2/4: a safety archive of the old data inside the DB.

Later (0139) the bundle-variant tables and the old FKs are deleted irreversibly. So that no
value is lost, BEFORE copying we make a full copy of everything that will be changed or
deleted into a separate schema `legacy_bundles` (no FKs and no link to Django models — the
ORM doesn't see it, and it doesn't affect the site).

Delete once the merge is verified on prod:
    DROP SCHEMA legacy_bundles CASCADE;
A single bundle can be restored from the archive with an ordinary SELECT (same columns as in
catalog_bundlevariant / catalog_bundle before the merge).
"""
from django.db import migrations

ARCHIVE_TABLES = [
    # (name in the archive, source)
    ('bundle', 'public.catalog_bundle'),
    ('bundlevariant', 'public.catalog_bundlevariant'),
    ('bundlevariant_attribute_values', 'public.catalog_bundlevariant_attribute_values'),
    ('bundlevariant_labels', 'public.catalog_bundlevariant_labels'),
    ('bundlevariantimage', 'public.catalog_bundlevariantimage'),
    ('bundleimage', 'public.catalog_bundleimage'),
    ('bundleitem', 'public.catalog_bundleitem'),
    ('bundleinstructionoverride', 'public.catalog_bundleinstructionoverride'),
    ('stocknotification', 'public.catalog_stocknotification'),
    ('favorite', 'public.catalog_favorite'),
]


def archive(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return  # the archive is Postgres-only (prod/dev); skipped on other databases
    ex = schema_editor.execute
    ex('CREATE SCHEMA IF NOT EXISTS legacy_bundles')
    ex("COMMENT ON SCHEMA legacy_bundles IS "
       "'Archive of bundle data before the BundleVariant→Bundle merge (catalog 0137). "
       "Can be deleted: DROP SCHEMA legacy_bundles CASCADE.'")
    for name, source in ARCHIVE_TABLES:
        ex(f'CREATE TABLE IF NOT EXISTS legacy_bundles.{name} AS TABLE {source}')


def noop_reverse(apps, schema_editor):
    """We keep the archive: rolling back the migration must not destroy the safety copy."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0136_bundle_merge_add_fields'),
    ]

    operations = [
        migrations.RunPython(archive, noop_reverse),
    ]
