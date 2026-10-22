"""
Signal that grants access to a course when an order is paid.

Logic:
- pre_save Order — record the previous payment_status in `_prev_payment_status`.
- post_save Order — on a pending → paid transition, create a CoursePurchase
  for each OrderItem of the order with course_id set.

The unique constraint (user, course) on CoursePurchase protects against a duplicate on
repeated bank webhooks.

Guest orders (order.user is None) are skipped — a course is bound to a user,
an anonymous purchase has nobody to grant access to. We log a WARN so that
a manager can manually link a CoursePurchase if the customer registers.
"""
import logging
from django.db import transaction
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver

from orders.models import Order
from .models import CoursePurchase

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Order)
def _order_capture_prev_payment(sender, instance, **kwargs):
    if instance.pk:
        prev = Order.objects.filter(pk=instance.pk).only('payment_status').first()
        instance._prev_payment_status = prev.payment_status if prev else None
    else:
        instance._prev_payment_status = None


def grant_course_access(order) -> int:
    """Grants CoursePurchase for all courses of the order. Idempotent.

    Called by the signal below, and also manually from places where payment_status
    is set via queryset.update(), bypassing post_save (catalog/views.py
    set_status in the "Payments" section). Returns the number of accesses created.
    """
    course_items = order.items.filter(course__isnull=False).select_related('course')
    if not course_items.exists():
        return 0

    if order.user_id is None:
        # Guest purchase — nobody to grant access to. The admin must manually link a
        # CoursePurchase if the customer registers. We log it so it does not get lost.
        course_names = ', '.join(item.course.name for item in course_items)
        logger.warning(
            'Guest order #%s paid for course(s) [%s] — no user to grant access. '
            'Customer email: %s. Manual CoursePurchase required.',
            order.order_number, course_names, order.customer_email,
        )
        return 0

    created_count = 0
    with transaction.atomic():
        for item in course_items:
            _, was_created = CoursePurchase.objects.get_or_create(
                user_id=order.user_id,
                course_id=item.course_id,
                defaults={'order_item': item},
            )
            created_count += int(was_created)
    return created_count


def revoke_course_access(order) -> int:
    """Revokes access granted by this order: the payment was rolled back or cancelled.

    We revoke ONLY what this very order granted (CoursePurchase.order_item points to its
    item), and only if the person has no other paid order with the same course — otherwise
    a refund of one purchase would take away access bought separately. Returns the number of
    accesses revoked.
    """
    if order.user_id is None:
        return 0
    course_ids = list(order.items.filter(course__isnull=False).values_list('course_id', flat=True))
    if not course_ids:
        return 0

    removed = 0
    with transaction.atomic():
        for course_id in course_ids:
            other_paid = (Order.objects
                          .filter(user_id=order.user_id, payment_status='paid',
                                  items__course_id=course_id)
                          .exclude(pk=order.pk)
                          .exclude(status='cancelled')
                          .exists())
            if other_paid:
                continue
            deleted, _ = (CoursePurchase.objects
                          .filter(user_id=order.user_id, course_id=course_id,
                                  order_item__order_id=order.pk)
                          .delete())
            removed += deleted
    if removed:
        logger.warning('Order #%s: revoked %d course access(es) — payment/status rolled back',
                       order.order_number, removed)
    return removed


@receiver(post_save, sender=Order)
def _course_grant_access_on_paid(sender, instance, created, **kwargs):
    prev = getattr(instance, '_prev_payment_status', None)
    new = instance.payment_status

    # Payment rolled back (paid → not paid) — we revoke access. We do not touch order cancellation
    # without a payment change: the money stayed with us, the person has seen the course.
    if prev == 'paid' and new != 'paid':
        revoke_course_access(instance)
        return

    # Only the pending → paid transition matters (including creating an order straight
    # as paid — a rare case of a manual admin, handled too).
    if new != 'paid':
        return
    if prev == 'paid':
        return

    grant_course_access(instance)
