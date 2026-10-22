"""Realtime counter of CRM notifications.

Changes to orders / bank payments affect the "Payments" section
of the notification counter — after commit we send a broadcast over WebSocket.
"""

from django.db import transaction
from django.db.models.signals import post_save, post_delete

from .models import Order, OrderItem, OrderItemBundleComponent, PaymentMatch, BankTransaction


def _notify_counts_changed(*args, **kwargs):
    from catalog.notifications import broadcast_notification_counts
    transaction.on_commit(broadcast_notification_counts)


for _ntf_model in (Order, PaymentMatch, BankTransaction):
    post_save.connect(_notify_counts_changed, sender=_ntf_model,
                      dispatch_uid=f'ntfcnt_save_{_ntf_model.__name__}')
    post_delete.connect(_notify_counts_changed, sender=_ntf_model,
                        dispatch_uid=f'ntfcnt_del_{_ntf_model.__name__}')


def _cleanup_order_notif_state(sender, instance, **kwargs):
    """Order deleted → remove its orphan notification states.
    NotificationState is not an FK to Order (just section+object_id), so the DB
    cascade does not clean it up — we do it manually."""
    from catalog.models import NotificationState
    NotificationState.objects.filter(
        section__in=('payments', 'requisites'), object_id=instance.id
    ).delete()


post_delete.connect(_cleanup_order_notif_state, sender=Order,
                    dispatch_uid='ntfstate_cleanup_order')


def _invalidate_items_facet(*args, **kwargs):
    """Reset the product facet cache (the filter popup in the order list) on any change to the
    items' composition, so a new/renamed item shows up in the filter right away.
    A bundle component (OrderItemBundleComponent) is also a facet source — editing the bundle
    composition in a single order does not touch the OrderItem itself, so without a separate
    signal the facet would not see the change until the TTL expires."""
    from django.core.cache import cache
    from .admin import OrderAdmin
    cache.delete(OrderAdmin.FACET_CACHE_KEY)


for _facet_model in (OrderItem, OrderItemBundleComponent):
    post_save.connect(_invalidate_items_facet, sender=_facet_model,
                      dispatch_uid=f'facet_inval_save_{_facet_model.__name__}')
    post_delete.connect(_invalidate_items_facet, sender=_facet_model,
                        dispatch_uid=f'facet_inval_del_{_facet_model.__name__}')
