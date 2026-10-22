"""Regressions for 3 fixes of stock write-off correctness (audit 2026-07):

  #1 set_status ("Payments" notifications) — for monobank it must write off/return stock,
     because queryset.update() doesn't trigger the _order_status_changed signal.
  #2 revert_invoice — under a transaction + lock; a repeated rollback doesn't hit stock twice.
  #3 an imported OLD order — cancelling/deleting does NOT inflate stock (stock was never
     written off on import).
"""
from __future__ import annotations

import json
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model

from catalog.models import Category, Product, ProductVariant
from orders.models import Order, OrderItem, Invoice, InvoiceItem

User = get_user_model()


def _variant(stock=10, price='100.00'):
    cat = Category.objects.create(name='Test')
    p = Product.objects.create(name='Test product')
    p.categories.add(cat)
    return ProductVariant.objects.create(product=p, price=Decimal(price), stock=stock)


def _order(**kw):
    defaults = dict(
        customer_name='Customer', customer_phone='+380000000000',
        delivery_type='nova_poshta', delivery_address='Branch No. 1',
        payment_method='cod', payment_status='pending', status='new',
    )
    defaults.update(kw)
    return Order.objects.create(**defaults)


# ─── #3: an imported OLD order does not inflate stock ────────────────────────────

class ImportedOrderRestoreTests(TestCase):
    def _imported_item(self, v, qty=3):
        """Mirrors the legacy order import: stock_applied=True, stock_consumed=NULL, stock is NOT moved."""
        o = _order(payment_method='cod', payment_status='paid', status='delivered',
                   is_imported=True)
        it = OrderItem.objects.create(
            order=o, product_variant=v, quantity=qty, price=Decimal('100'),
            stock_applied=True,
        )
        v.refresh_from_db()
        # sanity: the signal did not write off (stock_applied is already True → _consume is a no-op)
        self.assertEqual(v.stock, 10)
        self.assertIsNone(it.stock_consumed)
        return o, it

    def test_cancel_imported_order_does_not_inflate(self):
        v = _variant(stock=10)
        o, it = self._imported_item(v, qty=3)
        o.status = 'cancelled'
        o.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)  # NOT 13

    def test_delete_imported_item_does_not_inflate(self):
        v = _variant(stock=10)
        o, it = self._imported_item(v, qty=4)
        it.delete()
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)  # NOT 14

    def test_cancel_normal_order_still_restores(self):
        """Control: a regular (non-imported) order returns stock as before."""
        v = _variant(stock=10)
        o = _order(payment_method='cod', payment_status='paid', status='new')
        OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)   # written off on creation (cod)
        o.status = 'cancelled'
        o.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)  # returned


# ─── #1: set_status for monobank writes off/returns stock ───────────────────────

class MonobankSetStatusStockTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email='mgr@test.com', password='x', is_staff=True, is_active=True)
        self.client.force_login(self.staff)

    def _set_status(self, order, status):
        return self.client.post(
            '/admin/notifications/payment-action/',
            data=json.dumps({'action': 'set_status', 'id': order.id, 'status': status}),
            content_type='application/json',
        )

    def test_set_paid_consumes_then_pending_restores(self):
        v = _variant(stock=10)
        o = _order(payment_method='monobank', payment_status='pending', status='new')
        it = OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db(); it.refresh_from_db()
        self.assertEqual(v.stock, 10)          # monobank pending → not written off
        self.assertFalse(it.stock_applied)

        resp = self._set_status(o, 'paid')
        self.assertEqual(resp.status_code, 200, resp.content)
        v.refresh_from_db(); it.refresh_from_db()
        self.assertEqual(v.stock, 7)           # written off after paid
        self.assertTrue(it.stock_applied)

        resp = self._set_status(o, 'pending')
        self.assertEqual(resp.status_code, 200, resp.content)
        v.refresh_from_db(); it.refresh_from_db()
        self.assertEqual(v.stock, 10)          # returned after pending
        self.assertFalse(it.stock_applied)

    def test_set_paid_does_not_touch_cod(self):
        """Control: cod is written off on creation; set_status doesn't touch its stock."""
        v = _variant(stock=10)
        o = _order(payment_method='cod', payment_status='pending', status='new')
        OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)           # cod written off on creation
        resp = self._set_status(o, 'paid')
        self.assertEqual(resp.status_code, 200, resp.content)
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)           # no double write-off


# ─── #7: pure dropshipping doesn't write off the mirror stock; hybrid does ─────────────

class DropStockMirrorTests(TestCase):
    def test_pure_drop_stock_untouched(self):
        v = _variant(stock=2)  # mirror of the Bradas stock level
        v.is_dropshipping = True
        v.save(update_fields=['is_dropshipping'])
        o = _order(payment_method='cod', payment_status='pending', status='new')
        it = OrderItem.objects.create(order=o, product_variant=v, quantity=5, price=Decimal('100'))
        v.refresh_from_db(); it.refresh_from_db()
        self.assertEqual(v.stock, 2)                 # we don't touch the mirror (sale beyond the stock level)
        self.assertTrue(it.stock_applied)
        self.assertEqual(it.stock_consumed, Decimal('0'))
        o.status = 'cancelled'; o.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 2)                 # and it doesn't inflate on cancellation

    def test_hybrid_still_consumes_own_stock(self):
        v = _variant(stock=10)
        v.is_hybrid = True
        v.save(update_fields=['is_hybrid'])
        o = _order(payment_method='cod', payment_status='pending', status='new')
        OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)                 # hybrid writes off its own stock


# ─── #6: changing payment_method brings stock to the required state ───────────────

class PaymentMethodChangeStockTests(TestCase):
    def test_monobank_unpaid_to_cod_consumes(self):
        """Unpaid monobank (stock wasn't held) → cod: stock must be written off."""
        v = _variant(stock=10)
        o = _order(payment_method='monobank', payment_status='pending', status='new')
        OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)          # monobank pending → not written off
        o.payment_method = 'cod'
        o.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)           # cod → written off

    def test_cod_to_monobank_unpaid_restores(self):
        """cod (written off) → unpaid monobank: stock must be returned (held only after payment)."""
        v = _variant(stock=10)
        o = _order(payment_method='cod', payment_status='pending', status='new')
        OrderItem.objects.create(order=o, product_variant=v, quantity=3, price=Decimal('100'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)
        o.payment_method = 'monobank'
        o.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)          # returned


# ─── #2: a repeated invoice rollback doesn't hit stock twice ──────────────────────

class RevertIdempotencyTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email='mgr2@test.com', password='x', is_staff=True, is_active=True)
        self.client.force_login(self.staff)

    def test_second_revert_rejected_and_stock_intact(self):
        v = _variant(stock=10)
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='item', quantity=5, matched_variant=v)
        r = self.client.post(
            '/admin/orders/invoice/confirm/',
            data=json.dumps({'invoice_id': invoice.id, 'items': [
                {'id': it.id, 'variant_id': v.id, 'quantity': 5, 'remember_name': False}]}),
            content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        v.refresh_from_db()
        self.assertEqual(v.stock, 15)          # 10 + 5

        r1 = self.client.post(
            '/admin/orders/invoice/revert/',
            data=json.dumps({'invoice_id': invoice.id}), content_type='application/json')
        self.assertEqual(r1.status_code, 200, r1.content)
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)          # exactly one rollback

        # Second rollback: under the lock it sees status='reverted' → 400, stock is not touched.
        r2 = self.client.post(
            '/admin/orders/invoice/revert/',
            data=json.dumps({'invoice_id': invoice.id}), content_type='application/json')
        self.assertEqual(r2.status_code, 400)
        v.refresh_from_db()
        self.assertEqual(v.stock, 10)          # NOT 5 (the receipt was not subtracted a second time)
