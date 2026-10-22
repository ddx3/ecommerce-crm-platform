"""Signals of the User model.

Linking guest orders to a user:
when a User with an email is created, all Orders whose customer_email matches
the email and whose user_id is not yet set get this user_id.

Separate guard: on post_save for updated existing users this linking is NOT performed,
to avoid 'linking' on every PATCH /profile/. Trigger only on created=True.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import User


def link_guest_orders(user):
    """Attaches guest orders (user is null) to the account by email OR by the
    normalized phone (last 9 digits). The phone is the main channel, because the email
    is optional at checkout and most orders do not have one."""
    # Import inside the function, to avoid a circular orders <-> users import at startup.
    from orders.models import Order, normalize_phone, PLACEHOLDER_PHONES
    from django.db.models import Q
    conds = []
    if user.email:
        conds.append(Q(customer_email__iexact=user.email))
    ph9 = normalize_phone(getattr(user, 'phone', '') or '')
    if ph9 and ph9 not in PLACEHOLDER_PHONES:
        # Protection against family/shared numbers: if DIFFERENT people placed orders on the number
        # (several different names among the guest orders), we do NOT assign all the orders to
        # the first person who registers: we do not link by phone (we leave email + manual
        # linking in the CRM). Agreed with the family-numbers policy.
        names = {(n or '').strip().lower() for n in
                 Order.objects.filter(phone_normalized=ph9, user__isnull=True)
                 .values_list('customer_name', flat=True) if (n or '').strip()}
        if len(names) <= 1:
            conds.append(Q(phone_normalized=ph9))
    if not conds:
        return
    q = conds[0]
    for c in conds[1:]:
        q |= c
    Order.objects.filter(q, user__isnull=True).update(user=user)


@receiver(post_save, sender=User)
def link_guest_orders_on_user_create(sender, instance, created, **kwargs):
    # Only on creation (guard against linking on every PATCH /profile/).
    if not created:
        return
    link_guest_orders(instance)
