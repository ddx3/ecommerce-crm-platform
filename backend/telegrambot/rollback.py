"""Recording write effects and rolling them back.

Every tool write operation calls record(...) with a before/after snapshot.
apply_revert(entry) restores the state from these snapshots.

Decimal is not JSON-serializable, so prices are stored as strings and on rollback are passed
back as a string to the DecimalField (Django converts it itself).
"""
from __future__ import annotations

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from .models import RollbackEntry


def record(*, command_log_id, action, model_label, object_pk, before, after) -> RollbackEntry:
    return RollbackEntry.objects.create(
        command_id=command_log_id,
        action=action,
        model_label=model_label,
        object_pk=str(object_pk) if object_pk is not None else '',
        before=before,
        after=after,
    )


class RevertError(Exception):
    pass


def apply_revert(entry: RollbackEntry) -> str:
    """Rolls back a single RollbackEntry. Returns a human-readable description.

    Idempotent via the is_reverted flag (a repeated rollback is a no-op with a message).
    """
    if entry.is_reverted:
        return f'#{entry.pk} has already been rolled back'

    with transaction.atomic():
        if entry.action == 'create_product':
            msg = _revert_create_product(entry.after or {})
        elif entry.action == 'create_blog':
            msg = _revert_delete(entry.after or {}, 'blog_id', 'blog.BlogPost', 'post')
        elif entry.action == 'create_bundle':
            msg = _revert_delete(entry.after or {}, 'bundle_id', 'catalog.Bundle', 'bundle')
        elif entry.action.startswith('create_'):
            raise RevertError(f'Unknown create action for rollback: {entry.action}')
        else:
            # update_* and publish_*: restore the fields from before
            msg = _revert_update(entry)

        entry.is_reverted = True
        entry.reverted_at = timezone.now()
        entry.save(update_fields=['is_reverted', 'reverted_at'])
    return msg


def _revert_create_product(after: dict) -> str:
    """Deletes a created draft: images -> variant -> (the product is deleted by the signal
    if the variant was the only one)."""
    Product = apps.get_model('catalog', 'Product')
    ProductVariant = apps.get_model('catalog', 'ProductVariant')
    ProductImage = apps.get_model('catalog', 'ProductImage')

    image_ids = after.get('image_ids') or []
    variant_id = after.get('variant_id')
    product_id = after.get('product_id')

    ProductImage.objects.filter(id__in=image_ids).delete()
    if variant_id:
        # delete_product_if_no_variants (signal) will delete the product if this was the
        # last variant.
        ProductVariant.objects.filter(id=variant_id).delete()
    # Safety net: if the product is left without variants (e.g. it had no
    # variants at all), delete it explicitly.
    if product_id and not ProductVariant.objects.filter(product_id=product_id).exists():
        Product.objects.filter(id=product_id).delete()
    return f'Deleted the created product (product#{product_id})'


def _revert_delete(after: dict, id_key: str, model_label: str, human: str) -> str:
    """Deletes a created object (blog post / bundle) by the id from after."""
    app_label, model_name = model_label.split('.')
    Model = apps.get_model(app_label, model_name)
    obj_id = after.get(id_key)
    if obj_id:
        Model.objects.filter(pk=obj_id).delete()
    return f'Deleted the created {human} (#{obj_id})'


def _revert_update(entry: RollbackEntry) -> str:
    """Returns the object's fields to the values from before."""
    app_label, model_name = entry.model_label.split('.')
    Model = apps.get_model(app_label, model_name)
    obj = Model.objects.filter(pk=entry.object_pk).first()
    if obj is None:
        raise RevertError(f'{entry.model_label}#{entry.object_pk} not found')

    before = entry.before or {}
    fields = list(before.keys())
    for field, value in before.items():
        setattr(obj, field, value)
    # update_fields, so that the label/notification signals fire (auto_apply_labels
    # looks specifically at the changed fields).
    obj.save(update_fields=fields)
    return f'Rolled back {entry.model_label}#{entry.object_pk}: {before}'
