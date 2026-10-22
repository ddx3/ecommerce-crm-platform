"""Merge BundleVariant → Bundle, step 3/4: move data from the variant to the bundle.

Rules (they reproduce what the buyer saw before the merge):
- description: taken from the variant; if empty, the bundle's own stays (the storefront
  used to show `variant.description || bundle.description`). The old bundle-level
  description that was shadowed by the variant's description is not lost: it is appended
  to the AI draft (raw_text) with a marker; it is not shown anywhere on the site or in SEO
  (meta_* are filled);
- visibility: a bundle is active only if both it and its variant are active;
- name: if the variant has one, we take it (that is how the bundle page and the expanded
  card used to be titled, usually with the volume: "… for 10 L"); slug is untouched;
- SKU: the variant's, otherwise the bundle's; uniqueness is checked BEFORE writing.

The migration is guarded: if the data doesn't fit the '1 bundle = 1 variant' model (several
variants, duplicate SKU/subscriptions/favorites) it fails with an explanation, changing
nothing (the migration is atomic); at the end it checks every migrated field against the
source.
Updates go through .update(), so the bundles' `updated_at` does not shift.
"""
from django.db import migrations
from django.db.models import Count, OuterRef, Subquery

def _norm(text):
    """Name without extra spaces (double/leading/trailing) — so that we don't rename over a whitespace
difference."""
    return ' '.join((text or '').split())


LEGACY_SEP = chr(10)
LEGACY_MARK = ('<p><em>— Old general bundle description (before the variants were merged; '
               'it was not shown on the site because the variant description overrode it) —</em></p>')


def _merge_raw(b, bv):
    """AI draft: the variant's raw text, the bundle's raw text and — so that no text disappears
    from the live DB — the old general bundle description, if the variant's description
    shadowed it. Duplicates are not added."""
    parts = []
    for text in (bv.raw_text, b.raw_text):
        text = (text or '').strip()
        if text and text not in parts:
            parts.append(text)
    old_desc = (b.description or '').strip()
    shown = (bv.description or '').strip() or old_desc
    if old_desc and old_desc != shown and old_desc not in parts:
        parts.append(LEGACY_MARK + old_desc)
    return LEGACY_SEP.join(parts)


SCALARS = ('price', 'stock', 'discount_percent', 'discount_amount', 'application_norms',
           'instruction_html', 'instruction_map', 'instruction_stamp',
           'instruction_updated_at', 'sort_order')


def copy_data(apps, schema_editor):
    Bundle = apps.get_model('catalog', 'Bundle')
    BundleVariant = apps.get_model('catalog', 'BundleVariant')
    BundleItem = apps.get_model('catalog', 'BundleItem')
    BundleInstructionOverride = apps.get_model('catalog', 'BundleInstructionOverride')
    StockNotification = apps.get_model('catalog', 'StockNotification')
    Favorite = apps.get_model('catalog', 'Favorite')
    BundleImage = apps.get_model('catalog', 'BundleImage')

    # 0. The '1:1' model — otherwise some data would be lost silently.
    multi = sorted(Bundle.objects.annotate(n=Count('variants')).filter(n__gt=1)
                   .values_list('pk', flat=True))
    if multi:
        raise RuntimeError(f'Bundles with several variants: {multi}. '
                           f'Split them first (1 bundle = 1 variant).')

    bundles = {b.pk: b for b in Bundle.objects.all()}
    variants = {bv.bundle_id: bv for bv in BundleVariant.objects.all()}

    # 1. Final SKUs + uniqueness check before any write.
    final_sku, owner = {}, {}
    for pk, b in bundles.items():
        bv = variants.get(pk)
        sku = ((bv.sku if bv else None) or b.sku or None)
        final_sku[pk] = sku
        if sku:
            if sku in owner:
                raise RuntimeError(f'SKU {sku!r} is duplicated: bundles {owner[sku]} and {pk}')
            owner[sku] = pk

    # 2. Scalar fields. SKUs in two passes (first free them, then assign), so the intermediate
    # state doesn't hit the unique constraint even if SKUs 'move around'.
    changed_sku = [pk for pk, b in bundles.items() if final_sku[pk] != (b.sku or None)]
    if changed_sku:
        Bundle.objects.filter(pk__in=changed_sku).update(sku=None)
    for pk, b in bundles.items():
        bv = variants.get(pk)
        fields = {'sku': final_sku[pk]} if pk in changed_sku else {}
        if bv is not None:
            fields.update({f: getattr(bv, f) for f in SCALARS})
            fields['description'] = bv.description or b.description
            fields['raw_text'] = _merge_raw(b, bv)
            fields['is_active'] = bool(b.is_active and bv.is_active)
            vname = _norm(bv.name)
            if vname and vname != _norm(b.name):
                fields['name'] = vname
        if fields:
            Bundle.objects.filter(pk=pk).update(**fields)

    # 3. Labels and attributes.
    for pk, bv in variants.items():
        b = Bundle.objects.get(pk=pk)
        b.attribute_values.set(bv.attribute_values.all())
        b.labels.set(bv.labels.all())

    # 4. Child tables: bundle ← the variant's bundle.
    to_bundle = Subquery(BundleVariant.objects.filter(pk=OuterRef('bundle_variant_id'))
                         .values('bundle_id')[:1])
    BundleItem.objects.filter(bundle__isnull=True).update(bundle_id=to_bundle)
    BundleInstructionOverride.objects.filter(bundle__isnull=True).update(bundle_id=to_bundle)
    StockNotification.objects.filter(bundle_variant__isnull=False, bundle__isnull=True) \
        .update(bundle_id=to_bundle)
    Favorite.objects.filter(bundle_variant__isnull=False, bundle__isnull=True) \
        .update(bundle_id=to_bundle)
    img_to_bundle = Subquery(BundleVariant.objects.filter(pk=OuterRef('variant_id'))
                             .values('bundle_id')[:1])
    BundleImage.objects.filter(variant__isnull=False, bundle__isnull=True) \
        .update(bundle_id=img_to_bundle)

    # 5. Reconciliation: whatever didn't land stops the rollout instead of being lost silently.
    problems = []
    if BundleItem.objects.filter(bundle__isnull=True).exists():
        problems.append('composition items without a bundle')
    if BundleInstructionOverride.objects.filter(bundle__isnull=True).exists():
        problems.append('instruction overrides without a bundle')
    if StockNotification.objects.filter(variant__isnull=True, bundle__isnull=True).exists():
        problems.append('back-in-stock subscriptions without a target')
    if Favorite.objects.filter(bundle_variant__isnull=False, bundle__isnull=True).exists():
        problems.append('favorites without a bundle')
    if BundleImage.objects.filter(variant__isnull=False, bundle__isnull=True).exists():
        problems.append('variant photos without a bundle')
    for model, label, fields in (
        (StockNotification, 'subscription', ('email',)),
        (Favorite, 'favorite', ('device_id',)),
    ):
        dup = (model.objects.filter(bundle__isnull=False).values('bundle', *fields)
               .annotate(n=Count('pk')).filter(n__gt=1))
        if dup.exists():
            problems.append(f'duplicates ({label}) after the merge: {list(dup[:3])}')
    dup_ov = (BundleInstructionOverride.objects.values('bundle', 'signature')
              .annotate(n=Count('pk')).filter(n__gt=1))
    if dup_ov.exists():
        problems.append(f'duplicate instruction overrides: {list(dup_ov[:3])}')
    for pk, bv in variants.items():
        b = Bundle.objects.get(pk=pk)
        for f in SCALARS:
            if getattr(b, f) != getattr(bv, f):
                problems.append(f'bundle {pk}: field {f} does not match')
        if b.description != ((bv.description or '').strip() and bv.description or bundles[pk].description):
            problems.append(f'bundle {pk}: description does not match')
        if b.sku != (bv.sku or bundles[pk].sku or None):
            problems.append(f'bundle {pk}: SKU does not match')
        if b.is_active != bool(bundles[pk].is_active and bv.is_active):
            problems.append(f'bundle {pk}: visibility does not match')
        if set(b.labels.values_list('pk', flat=True)) != set(bv.labels.values_list('pk', flat=True)):
            problems.append(f'bundle {pk}: labels do not match')
        if set(b.attribute_values.values_list('pk', flat=True)) != \
                set(bv.attribute_values.values_list('pk', flat=True)):
            problems.append(f'bundle {pk}: attributes do not match')
        if BundleItem.objects.filter(bundle_id=pk).count() != \
                BundleItem.objects.filter(bundle_variant_id=bv.pk).count():
            problems.append(f'bundle {pk}: composition does not match')
    if problems:
        raise RuntimeError('Bundle merge stopped, nothing was changed: ' + '; '.join(problems[:10]))

    renamed = [(pk, bundles[pk].name, _norm(bv.name)) for pk, bv in variants.items()
               if _norm(bv.name) and _norm(bv.name) != _norm(bundles[pk].name)]
    for pk, old, new in renamed:
        if Bundle.objects.get(pk=pk).name != new:
            problems.append(f'bundle {pk}: name was not carried over')
    if problems:
        raise RuntimeError('Bundle merge stopped, nothing was changed: ' + '; '.join(problems[:10]))
    print(f'\n  [bundle merge] migrated: {len(variants)} bundles; '
          f'without a variant (left as is): {len(bundles) - len(variants)}; '
          f'name taken from the variant for {len(renamed)}')
    for pk, old, new in renamed:
        print(f'    bundle {pk}: "{old}" → "{new}"')


def noop_reverse(apps, schema_editor):
    """Nothing is restored on rollback: the old data stays in BundleVariant until step 0139."""


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0137_bundle_merge_archive_legacy'),
    ]

    operations = [
        migrations.RunPython(copy_data, noop_reverse),
    ]
