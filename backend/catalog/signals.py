import os
import threading

import requests
from django.db import transaction
from django.db.models import Q
from django.db.models.signals import post_save, post_delete, m2m_changed
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta

from .models import (
    ProductVariant, VariantLabel, Label, StockNotification,
    Review, DropSyncEvent, NotificationState,
    Product, Bundle, Category, BundleItem, Favorite,
)


def _get_label(slug):
    try:
        return Label.objects.get(slug=slug)
    except Label.DoesNotExist:
        return None


@receiver(post_delete, sender=ProductVariant)
def delete_product_if_no_variants(sender, instance, **kwargs):
    """If the last packaging was deleted, delete the product itself too."""
    try:
        product = instance.product
        if not product.variants.exists():
            product.delete()
    except Exception:
        pass


_LABEL_RELEVANT_FIELDS = {'stock', 'is_dropshipping', 'is_hybrid', 'is_active', 'discount_percent', 'discount_amount', 'old_price'}


@receiver(post_save, sender=ProductVariant)
def auto_apply_labels(sender, instance, created, update_fields=None, **kwargs):
    """Run the heavy logic only if relevant fields changed.
    This cuts the query cascade on every save (e.g. an update of only price/sku/etc)."""
    if not created and update_fields is not None and not (set(update_fields) & _LABEL_RELEVANT_FIELDS):
        return
    _handle_novynka(instance, created)
    _handle_pid_zamovlennya(instance)
    _handle_hybrid_pid_zamovlennya(instance)
    _handle_aktsia(instance)
    _handle_stock_notifications(instance, created)


def _handle_stock_notifications(variant, created):
    """If stock became > 0, send emails to subscribers of the packaging and of the bundles
    it is part of (bundle availability is computed from components)."""
    if created:
        return
    if variant.stock <= 0:
        return
    _queue_stock_notifications(
        Q(variant=variant) | Q(bundle__items__product_variant=variant)
    )


def _queue_stock_notifications(target_q):
    """Sending happens after commit: inside a transaction (a supplier invoice with several
    lines) the bundle may still be incomplete, and a rollback must not leave an email sent.
    exists() is a cheap filter so we don't attach on_commit to every save."""
    pending = StockNotification.objects.filter(target_q, is_notified=False)
    if not pending.exists():
        return
    ids = list(pending.values_list('pk', flat=True).distinct())
    # robust: a mailing failure is only logged — the stock save is already committed,
    # and the request (invoice, order) must not fail with a 500 because of an email.
    transaction.on_commit(
        lambda: StockNotification.send_due(StockNotification.objects.filter(pk__in=ids)),
        robust=True,
    )


@receiver(post_save, sender=Bundle)
def bundle_stock_notifications(sender, instance, created, **kwargs):
    # Explicit bundle stock or showing it again.
    if not created:
        _queue_stock_notifications(Q(bundle=instance))


@receiver(post_save, sender=BundleItem)
@receiver(post_delete, sender=BundleItem)
def bundle_item_stock_notifications(sender, instance, **kwargs):
    # A stock change (including auto-replacement of a packaging that ran out) changes availability.
    _queue_stock_notifications(Q(bundle_id=instance.bundle_id))


def _handle_novynka(variant, created):
    """Automatically add the "New" label when a variant is created. Duration — label.auto_days.
    Dropshipping products are NOT marked as new — it is someone else's catalog, not our new arrival."""
    if not created:
        return
    if getattr(variant, 'is_dropshipping', False):
        return
    label = _get_label(Label.SLUG_NOVYNKA)
    if label is None:
        return
    expires = timezone.now() + timedelta(days=label.auto_days) if label.auto_days else None
    VariantLabel.objects.get_or_create(
        variant=variant,
        label=label,
        defaults={'expires_at': expires, 'is_auto': True},
    )


def _handle_hybrid_pid_zamovlennya(variant):
    """
    For 50/50 variants: if stock==0, auto-add pid-zamovlennya,
    if stock>0, auto-remove it. Don't touch manual labels (is_auto=False).
    """
    if not variant.is_hybrid:
        return
    # A dropshipping variant is always "Made to order" — it is managed by
    # _handle_pid_zamovlennya.
    # If a variant is both dropshipping and hybrid, don't let the hybrid branch remove the label
    # when stock>0.
    if variant.is_dropshipping:
        return
    label = _get_label(Label.SLUG_PID_ZAMOVLENNYA)
    if label is None:
        return

    if variant.stock == 0:
        VariantLabel.objects.get_or_create(
            variant=variant,
            label=label,
            defaults={'expires_at': None, 'is_auto': True},
        )
    else:
        VariantLabel.objects.filter(
            variant=variant,
            label=label,
            is_auto=True,
        ).delete()


def _handle_aktsia(variant):
    """
    The 'Sale' label is set automatically on variants with a discount:
    - discount_percent != None, or
    - discount_amount != None, or
    - old_price != None.
    If the label was assigned manually by an admin (is_auto=False), don't touch it.
    """
    label = _get_label(Label.SLUG_AKTSIA)
    if label is None:
        return

    # The criterion must match the actual price reduction in effective_price:
    # percent only in the range (0;100), fixed discount > 0, old price strictly > current.
    # Otherwise zero/empty values (discount_percent=0, old_price==price) would attach
    # the "Sale" badge with no actual discount.
    pct = variant.discount_percent
    has_discount = (
        (pct is not None and 0 < pct < 100)
        or (variant.discount_amount is not None and variant.discount_amount > 0)
        or (
            variant.old_price is not None
            and variant.price is not None
            and variant.old_price > variant.price
        )
    )

    if has_discount:
        VariantLabel.objects.get_or_create(
            variant=variant,
            label=label,
            defaults={'expires_at': None, 'is_auto': True},
        )
    else:
        VariantLabel.objects.filter(
            variant=variant,
            label=label,
            is_auto=True,
        ).delete()


_BUNDLE_LABEL_FIELDS = {'price', 'discount_percent', 'discount_amount'}


@receiver(post_save, sender=Bundle)
def auto_apply_bundle_aktsia(sender, instance, created, update_fields=None, **kwargs):
    """
    The 'Sale' label on a bundle — as with products, automatic based on the fact of a
    discount.
    Criterion = get_old_price() is not None (consistent with the storefront price/badge: the
    bundle's
    discount or the discount from components). Bundle.labels is a direct M2M (no through with
    is_auto), so we add/remove it directly.
    """
    if not created and update_fields is not None and not (set(update_fields) & _BUNDLE_LABEL_FIELDS):
        return
    label = _get_label(Label.SLUG_AKTSIA)
    if label is None:
        return
    try:
        has_discount = instance.get_old_price() is not None
    except Exception:
        has_discount = (
            instance.discount_percent is not None
            or instance.discount_amount is not None
        )
    if has_discount:
        instance.labels.add(label)
    else:
        instance.labels.remove(label)


def _handle_pid_zamovlennya(variant):
    """
    The 'Made to order' label is set automatically only on dropshipping variants.
    If the label was assigned manually by an admin (is_auto=False), don't touch it.
    """
    label = _get_label(Label.SLUG_PID_ZAMOVLENNYA)
    if label is None:
        return

    if variant.is_dropshipping:
        VariantLabel.objects.get_or_create(
            variant=variant,
            label=label,
            defaults={'expires_at': None, 'is_auto': True},
        )
    else:
        # Remove the auto-label if the variant is not dropshipping
        VariantLabel.objects.filter(
            variant=variant,
            label=label,
            is_auto=True,
        ).delete()


def recompute_stock_labels(variant):
    """Recompute a variant's stock-dependent auto-labels when stock was changed BYPASSING
    save() — a queryset .update()/F() when stock is written off/returned through an order
    (orders._consume_variant/_restore_amount). The post_save signal doesn't fire there,
    so we call the handler directly. Mirrors what BulkSource.sync_variants() does
    for bulk-bag packagings via v.save(update_fields=['stock', ...]).
    The only stock-dependent label is a hybrid's "Made to order"; the dropshipping
    label is static."""
    if variant is None or not getattr(variant, 'is_hybrid', False):
        return
    try:
        variant.refresh_from_db(fields=['stock'])
    except ProductVariant.DoesNotExist:
        return
    _handle_hybrid_pid_zamovlennya(variant)


# ─────────────────────────────────────────────────────────────────────
#  Realtime CRM notification counter.
#  Any data change that affects the counter → broadcast over WebSocket.
#  on_commit: broadcast only after a successful commit (not on rollback).
# ─────────────────────────────────────────────────────────────────────
def _notify_counts_changed(*args, **kwargs):
    from .notifications import broadcast_notification_counts
    transaction.on_commit(broadcast_notification_counts)


for _ntf_model in (Review, StockNotification, DropSyncEvent, NotificationState):
    post_save.connect(_notify_counts_changed, sender=_ntf_model,
                      dispatch_uid=f'ntfcnt_save_{_ntf_model.__name__}')
    post_delete.connect(_notify_counts_changed, sender=_ntf_model,
                        dispatch_uid=f'ntfcnt_del_{_ntf_model.__name__}')


@receiver(post_save, sender=Favorite)
@receiver(post_delete, sender=Favorite)
def _favorite_counts_changed(sender, **kwargs):
    """Likes are also counted in "Subscriptions", but they are set from the public API
    (sync of old likes — in batches of up to 500 rows): a full recount of the counters on
    every row would cost a customer request. At most once per 10 s — the skipped ones are
    picked up by the next broadcast or the sidebar counter cache."""
    from django.core.cache import cache
    if cache.add('ntf_counts_favorite_throttle', 1, 10):
        _notify_counts_changed()


# Notification source → NotificationState sections that must be removed when it is
# deleted (NotificationState is not an FK, so the DB cascade doesn't fire).
_NOTIF_SECTION_MODELS = {
    Review:            ('reviews', 'questions'),
    StockNotification: ('stock',),
    DropSyncEvent:     ('dropsync',),
    Favorite:          ('stock_like',),
}


def _cleanup_notif_state(sender, instance, **kwargs):
    sections = _NOTIF_SECTION_MODELS.get(sender)
    if not sections:
        return
    NotificationState.objects.filter(section__in=sections, object_id=instance.id).delete()


for _src_model in _NOTIF_SECTION_MODELS:
    post_delete.connect(_cleanup_notif_state, sender=_src_model,
                        dispatch_uid=f'ntfstate_cleanup_{_src_model.__name__}')


# ─────────────────────────────────────────────────────────────────────
#  LLM moderation of new reviews / comments / questions (all via Review)
#  Run in a separate thread after commit so as not to block the request.
# ─────────────────────────────────────────────────────────────────────
def _run_review_moderation(pk: int):
    import threading

    def _job():
        from .llm_moderation import moderate
        try:
            obj = Review.objects.get(pk=pk)
        except Review.DoesNotExist:
            return
        if obj.moderation_verdict:
            return
        # If rating is set, it's a review; otherwise a question/comment.
        kind = 'review' if obj.rating else 'question'
        res = moderate(kind=kind, text=obj.text, rating=obj.rating)
        update_fields = ['moderation_verdict', 'moderation_reason']
        obj.moderation_verdict = res.verdict
        obj.moderation_reason = res.reason
        if res.verdict == 'pass':
            obj.is_approved = True
            update_fields.append('is_approved')
        obj.save(update_fields=update_fields)

    threading.Thread(target=_job, daemon=True).start()


@receiver(post_save, sender=Review)
def moderate_review_on_create(sender, instance, created, **kwargs):
    if not created:
        return
    # Manager replies (parent != None) and already approved ones (staff) are not moderated.
    if instance.parent_id or instance.is_approved:
        return
    transaction.on_commit(lambda: _run_review_moderation(instance.pk))


# ─────────────────────────────────────────────────────────────────────
#  Flora (MyPlantApp) — notify the assistant when a product's TEXT changed,
#  so it re-embeds exactly this unit. Text only (name/description/composition): price and
#  stock are served to Flora in realtime, so no signal is needed for them — and it
#  wouldn't fire anyway, since sales change stock via .update(), bypassing post_save.
#  Best-effort, async after commit; the safety net is the daily delta cron.
# ─────────────────────────────────────────────────────────────────────
# is_active included: a deactivation must reach Flora so it purges the unit (Flora's
# upsert_by_id deletes on a 'missing' response, and the batch endpoint filters is_active).
_FLORA_PRODUCT_TEXT = {'name', 'description', 'application_norms', 'is_active'}
_FLORA_BUNDLE_TEXT = {'name', 'description', 'application_norms', 'is_active'}


def _notify_flora(unit_id: str):
    url = os.getenv('FLORA_WEBHOOK_URL', '')
    secret = os.getenv('FLORA_WEBHOOK_SECRET', '')
    if not url or not secret:
        return

    def _job():
        try:
            requests.post(url, json={'ids': [unit_id]},
                          headers={'X-Cron-Secret': secret}, timeout=5)
        except Exception:
            pass

    transaction.on_commit(lambda: threading.Thread(target=_job, daemon=True).start())


@receiver(post_save, sender=Product)
def flora_product_changed(sender, instance, created, update_fields=None, **kwargs):
    if not created and update_fields is not None and not (set(update_fields) & _FLORA_PRODUCT_TEXT):
        return
    _notify_flora(f'prod-{instance.pk}')


@receiver(post_save, sender=Bundle)
def flora_bundle_changed(sender, instance, created, update_fields=None, **kwargs):
    if not created and update_fields is not None and not (set(update_fields) & _FLORA_BUNDLE_TEXT):
        return
    _notify_flora(f'bundle-{instance.pk}')


@receiver(post_save, sender=BundleItem)
@receiver(post_delete, sender=BundleItem)
def flora_bundleitem_changed(sender, instance, **kwargs):
    # Bundle contents → embed_text; BundleItem has no timestamp, so the delta cron can't
    # see it — this webhook is its only signal.
    bid = getattr(instance, 'bundle_id', None)
    if bid:
        _notify_flora(f'bundle-{bid}')


@receiver(post_delete, sender=Product)
def flora_product_deleted(sender, instance, **kwargs):
    # Deletion → tell Flora to purge the unit. instance.pk is still set in post_delete and
    # the f-string is evaluated now (captured in the on_commit closure). Flora's
    # upsert_by_id sees the id 'missing' and calls delete_unit.
    _notify_flora(f'prod-{instance.pk}')


@receiver(post_delete, sender=Bundle)
def flora_bundle_deleted(sender, instance, **kwargs):
    _notify_flora(f'bundle-{instance.pk}')


# ── Category normalization: remove redundant ancestors ──────────────────────
#
# Linking a product/bundle to a descendant already shows it in all ancestors (the storefront
# filters by subtree — get_subtree_ids_self). Keeping the ancestor among the linked ones
# too is redundant: it duplicates the link and corrupts data when a category is moved in
# the tree (the old ancestor stays hanging). So when the selected set contains both an ancestor
# and its descendant, we remove the ancestor. A bare ancestor (with no descendant among the
# selected ones) stays — it is a valid direct link.
#
# Caught on m2m_changed, so it works regardless of the source: admin forms,
# import_products/import_bundles/import_dropshipping, sync_dropshipping,
# product cloning, the AI bot in Telegram — all of them go through the same M2M manager.

def _prune_redundant_ancestor_categories(instance):
    cat_ids = set(instance.categories.values_list('id', flat=True))
    if len(cat_ids) < 2:
        return
    parent_of = dict(Category.objects.values_list('id', 'parent_id'))
    redundant = set()
    for cid in cat_ids:
        p = parent_of.get(cid)
        while p is not None:
            if p in cat_ids:
                redundant.add(p)
            p = parent_of.get(p)
    if redundant:
        # .remove() sends post_remove, not post_add — there is no recursion.
        instance.categories.remove(*redundant)


def _prune_categories_on_change(instance, action, reverse):
    # Only when added (post_add). The reverse side (category.products.add) is not
    # used in the code — instance there would be a Category, so we ignore it.
    if action != 'post_add' or reverse:
        return
    _prune_redundant_ancestor_categories(instance)


@receiver(m2m_changed, sender=Product.categories.through)
def product_categories_prune(sender, instance, action, reverse, **kwargs):
    _prune_categories_on_change(instance, action, reverse)


@receiver(m2m_changed, sender=Bundle.categories.through)
def bundle_categories_prune(sender, instance, action, reverse, **kwargs):
    _prune_categories_on_change(instance, action, reverse)


# ── Cache of the "Products" list (ProductAdmin.changelist_view) ─────────────────────────
# The full slice of packagings is cached for VARIANTS_CL_TTL (catalog/admin.py) — here
# we invalidate it right after a regular edit (save/delete), so the manager
# sees their change right after saving the form rather than waiting for the TTL. The TTL stays
# as a fallback for bulk-.update() paths that bypass the signal.
def _invalidate_variants_cl_cache(*args, **kwargs):
    from django.core.cache import cache
    from .admin import ProductAdmin
    cache.delete(ProductAdmin.VARIANTS_CL_CACHE_KEY)


for _vc_model in (Product, ProductVariant):
    post_save.connect(_invalidate_variants_cl_cache, sender=_vc_model,
                       dispatch_uid=f'variants_cl_inval_save_{_vc_model.__name__}')
    post_delete.connect(_invalidate_variants_cl_cache, sender=_vc_model,
                         dispatch_uid=f'variants_cl_inval_del_{_vc_model.__name__}')
