"""Wipe & re-seed notifications for UI testing.

Clears:
  • unapproved Review rows (parent IS NULL only)
  • unanswered Question rows
  • all StockNotification rows
  • all DropSyncEvent rows
  • Order.payment_notification flags (orders themselves are kept)

Then creates a few fresh test rows in each category, using whatever
products/variants exist in the DB.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import (
    Bundle, DropSyncEvent, Product, ProductVariant, Review,
    StockNotification,
)
from orders.models import Order


REVIEWS = [
    ('Jane Doe',   'jane@example.com', 5, 'Very good quality, delivered quickly!'),
    ('John Roe',   'john@example.com', 3, 'Okay, but I expected better coloring quality.'),
    ('Anonymous',  '',                 1, 'It broke after the first use, please refund my money!'),
]

# Site reviews — not tied to a product/bundle.
SITE_REVIEWS = [
    ('Kate Sample', 'kate@example.com', 5, 'This is not my first order — everything arrives on time and the packaging is intact. I recommend it to friends.'),
    ('Sam M.',       'sam@example.com',  4, 'The site is convenient, but search sometimes lags on mobile. Overall, I recommend it.'),
    ('Anonymous',    '',                 2, 'The manager took a long time to answer in the chat. I did get my order in the end, but the impression is spoiled.'),
]

QUESTIONS = [
    ('Maria Sample', 'maria@example.com', 'Could you tell me whether this product is suitable for houseplants?'),
    ('Alex',          '',                  'What is the mesh density? And can it be laid under a lawn?'),
]

# Questions/comments about the site itself (no product).
SITE_QUESTIONS = [
    ('Natalie', 'natalie@example.com', 'Do you accept non-cash payment from legal entities? I need an invoice.'),
]

STOCK_SUBS = [
    'kateryna@example.com',
    'andriy@example.com',
    'olena@example.com',
]

DROPSYNC = [
    (DropSyncEvent.KIND_NEW_PRODUCT,  'NEW-001', 'Bradas — Leaf rake'),
    (DropSyncEvent.KIND_PRICE_CHANGE, 'PR-CH-2', 'Bradas — Fence netting'),
    (DropSyncEvent.KIND_NEW_VARIANT,  'NV-003',  'Bradas — Border edging (new height)'),
    (DropSyncEvent.KIND_RETURNED,     'RET-004', 'Bradas — Pegs, 100 pcs'),
]


class Command(BaseCommand):
    help = 'Wipe & re-seed notifications (reviews/questions/stock/dropsync/payments) for UI testing.'

    @transaction.atomic
    def handle(self, *args, **opts):
        # ── Wipe ────────────────────────────────────────────────────────
        deleted_reviews   = Review.objects.filter(is_approved=False, parent__isnull=True, rating__isnull=False).delete()[0]
        deleted_questions = Review.objects.filter(is_approved=False, parent__isnull=True, rating__isnull=True).delete()[0]
        deleted_stock     = StockNotification.objects.all().delete()[0]
        deleted_dropsync  = DropSyncEvent.objects.all().delete()[0]
        cleared_payments  = Order.objects.filter(payment_notification=True).update(payment_notification=False)

        self.stdout.write(self.style.WARNING(
            f'Wiped: reviews={deleted_reviews}, questions={deleted_questions}, '
            f'stock={deleted_stock}, dropsync={deleted_dropsync}, '
            f'payment-flags-cleared={cleared_payments}'
        ))

        # ── Pick anchors (first available product / variant / bundle) ──
        product = Product.objects.order_by('pk').first()
        bundle  = Bundle.objects.order_by('pk').first()
        variants = list(ProductVariant.objects.order_by('pk')[:5])
        order   = Order.objects.order_by('-pk').first()

        if not product and not bundle:
            self.stdout.write(self.style.ERROR('No products/bundles in DB — cannot seed reviews/questions'))
            return

        target = product or bundle  # pick whichever exists; FK either product or bundle
        target_kw = {'product': product} if product else {'bundle': bundle}

        # ── Reviews + demo dialogs (nested replies) ─────────────────
        # First review — simple, no replies.
        # Second — with a manager reply.
        # Third — multi-level thread (manager ↔ customer).
        created_reviews = []
        for name, email, rating, text in REVIEWS:
            r = Review.objects.create(
                author_name=name, author_email=email,
                rating=rating, text=text,
                is_approved=False,
                **target_kw,
            )
            created_reviews.append(r)

        if len(created_reviews) >= 2:
            r2 = created_reviews[1]
            Review.objects.create(
                parent=r2, product=r2.product, bundle=r2.bundle,
                author_name='Shop', author_email='', text='Thank you for the review! We will pass your comments on the coloring quality on to production.',
                is_approved=True,
            )
        if len(created_reviews) >= 3:
            r3 = created_reviews[2]
            mgr = Review.objects.create(
                parent=r3, product=r3.product, bundle=r3.bundle,
                author_name='Shop', author_email='', text='Hello! We apologize. Please contact support — we will arrange a refund.',
                is_approved=True,
            )
            client = Review.objects.create(
                parent=mgr, product=mgr.product, bundle=mgr.bundle,
                author_name='Anonymous', author_email='', text='Thank you, I wrote to support. Waiting for a reply.',
                is_approved=True,
            )
            Review.objects.create(
                parent=client, product=client.product, bundle=client.bundle,
                author_name='Shop', author_email='', text='We have replied to you by email — please check your inbox (including spam).',
                is_approved=True,
            )

        # ── Questions (Review without rating) + demo replies ──────────────
        created_questions = []
        for name, email, text in QUESTIONS:
            q = Review.objects.create(
                author_name=name, author_email=email,
                text=text, rating=None,
                is_approved=False,
                **target_kw,
            )
            created_questions.append(q)

        if len(created_questions) >= 1:
            q1 = created_questions[0]
            Review.objects.create(
                parent=q1, product=q1.product, bundle=q1.bundle,
                author_name='Shop', author_email='', text='Yes, this product is suitable for houseplants. Dosage: 5 ml per liter of water, once every 2 weeks.',
                is_approved=True,
            )

        # ── Site reviews (no product/bundle) ──────────────────────
        for name, email, rating, text in SITE_REVIEWS:
            Review.objects.create(
                author_name=name, author_email=email,
                rating=rating, text=text,
                is_approved=False,
            )
        # Site question (no product, no rating)
        for name, email, text in SITE_QUESTIONS:
            sq = Review.objects.create(
                author_name=name, author_email=email,
                text=text, rating=None, is_approved=False,
            )
            Review.objects.create(
                parent=sq, author_name='Shop', author_email='',
                text='Yes, we work with legal entities. Write to office@example.com — we will prepare an invoice.',
                is_approved=True,
            )

        # ── Stock subscriptions (need a variant) ────────────────────────
        if variants:
            for i, email in enumerate(STOCK_SUBS):
                StockNotification.objects.create(
                    email=email,
                    variant=variants[i % len(variants)],
                    is_notified=False,
                )
        else:
            self.stdout.write(self.style.WARNING('  No variants in DB — skipped stock subscriptions'))

        # ── DropSync events ─────────────────────────────────────────────
        for kind, sku, msg in DROPSYNC:
            DropSyncEvent.objects.create(
                kind=kind, supplier_sku=sku, message=msg,
                variant=variants[0] if variants else None,
                is_seen=False,
            )

        # ── Payment notification (mark latest order) ────────────────────
        if order:
            order.payment_notification = True
            order.save(update_fields=['payment_notification'])
            self.stdout.write(f'  Flagged order #{order.pk} as payment-notification')
        else:
            self.stdout.write(self.style.WARNING('  No orders in DB — skipped payment notification'))

        self.stdout.write(self.style.SUCCESS(
            f'Created: reviews={len(REVIEWS)}, questions={len(QUESTIONS)}, '
            f'stock={len(STOCK_SUBS) if variants else 0}, dropsync={len(DROPSYNC)}, '
            f'payments={1 if order else 0}'
        ))
