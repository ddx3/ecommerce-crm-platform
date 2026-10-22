"""Tests of the supplier invoice flow: confirmation (adding to stock) and rollback.
Special focus — duplicates (the same product in two rows / from two photos)."""
from __future__ import annotations

import json
from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.db.models import F

from catalog.models import Category, Product, ProductVariant
from orders.models import Invoice, InvoiceItem, InvoiceNameMapping

User = get_user_model()


def _variant(stock=10, price='100.00'):
    cat = Category.objects.create(name='Test')
    p = Product.objects.create(name='Test product')
    p.categories.add(cat)
    return ProductVariant.objects.create(product=p, price=Decimal(price), stock=stock)


class InvoiceConfirmRevertTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            email='mgr@test.com', password='x', is_staff=True, is_active=True,
        )
        self.client.force_login(self.staff)

    def _confirm(self, payload):
        return self.client.post(
            '/admin/orders/invoice/confirm/', data=json.dumps(payload),
            content_type='application/json',
        )

    def _revert(self, payload):
        return self.client.post(
            '/admin/orders/invoice/revert/', data=json.dumps(payload),
            content_type='application/json',
        )

    def test_duplicate_lines_same_variant_add_and_revert(self):
        """Two rows for one product: stock is summed, and the rollback restores the ORIGINAL
        value (not the intermediate 'step')."""
        v = _variant(stock=10, price='100.00')
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it1 = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=3, matched_variant=v)
        it2 = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=5, matched_variant=v)

        resp = self._confirm({
            'invoice_id': invoice.id,
            'items': [
                {'id': it1.id, 'variant_id': v.id, 'quantity': 3, 'remember_name': False},
                {'id': it2.id, 'variant_id': v.id, 'quantity': 5, 'remember_name': False},
            ],
        })
        self.assertEqual(resp.status_code, 200, resp.content)

        v.refresh_from_db()
        self.assertEqual(v.stock, 18)  # 10 + 3 + 5

        # The 'before' snapshot is identical and original on both rows — otherwise the rollback
        # breaks.
        it1.refresh_from_db(); it2.refresh_from_db()
        self.assertEqual(it1.original_stock, 10)
        self.assertEqual(it2.original_stock, 10)

        resp = self._revert({'invoice_id': invoice.id})
        self.assertEqual(resp.status_code, 200, resp.content)

        v.refresh_from_db()
        self.assertEqual(v.stock, 10)  # full restoration, not 13

    def test_sale_between_confirm_and_revert_is_preserved(self):
        """Phantom stock bug: sales happened between confirmation and rollback.
        The rollback must SUBTRACT exactly the stock receipt, not restore the absolute value —
        otherwise sales
        are erased and the product 'comes back' into stock that physically doesn't exist."""
        v = _variant(stock=10, price='100.00')
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=5, matched_variant=v)

        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 5, 'remember_name': False}],
        })
        v.refresh_from_db()
        self.assertEqual(v.stock, 15)  # 10 + 5

        # A sale of 4 pcs between confirmation and rollback (the write-off bypasses save, as in
        # production).
        ProductVariant.objects.filter(pk=v.pk).update(stock=F('stock') - 4)

        self._revert({'invoice_id': invoice.id})
        v.refresh_from_db()
        # Correct: 15 - 4 (sale) - 5 (stock receipt removed) = 6. NOT 10 (the old bug erased the
        # sale).
        self.assertEqual(v.stock, 6)

    def test_reapply_cycle_after_sales_does_not_inflate(self):
        """Full user cycle: applied → sales → rollback → applied again.
        Stock must not inflate by the quantity sold in the meantime."""
        v = _variant(stock=2, price='100.00')
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=10, matched_variant=v)

        # 1) Applied: 2 + 10 = 12
        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 10, 'remember_name': False}],
        })
        v.refresh_from_db(); self.assertEqual(v.stock, 12)

        # 2) Sold 4: 12 - 4 = 8
        ProductVariant.objects.filter(pk=v.pk).update(stock=F('stock') - 4)

        # 3) Rollback: 8 - 10 of receipt = -2 → 0 (not below zero)
        self._revert({'invoice_id': invoice.id})
        v.refresh_from_db(); self.assertEqual(v.stock, 0)

        # 4) Edits and re-apply: 0 + 10 = 10 (not 12 as with the old bug)
        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 10, 'remember_name': False}],
        })
        v.refresh_from_db(); self.assertEqual(v.stock, 10)

    def test_duplicate_lines_price_revert(self):
        """Duplicates with a manual price: the price rollback restores the original, not the intermediate one."""
        v = _variant(stock=10, price='100.00')
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it1 = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=1, matched_variant=v)
        it2 = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=1, matched_variant=v)

        resp = self._confirm({
            'invoice_id': invoice.id,
            'items': [
                {'id': it1.id, 'variant_id': v.id, 'quantity': 1, 'price': '150.00', 'remember_name': False},
                {'id': it2.id, 'variant_id': v.id, 'quantity': 1, 'price': '120.00', 'remember_name': False},
            ],
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        v.refresh_from_db()
        self.assertEqual(v.price, Decimal('120.00'))  # last manual price

        it1.refresh_from_db(); it2.refresh_from_db()
        self.assertEqual(it1.original_price, Decimal('100.00'))
        self.assertEqual(it2.original_price, Decimal('100.00'))

        resp = self._revert({'invoice_id': invoice.id, 'revert_price': True})
        self.assertEqual(resp.status_code, 200, resp.content)
        v.refresh_from_db()
        self.assertEqual(v.price, Decimal('100.00'))  # original, not 150

    def test_single_line_add_and_revert(self):
        """Basic case without duplicates — not broken."""
        v = _variant(stock=7, price='50.00')
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='goods', quantity=4, matched_variant=v)

        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 4, 'remember_name': False}],
        })
        v.refresh_from_db()
        self.assertEqual(v.stock, 11)

        self._revert({'invoice_id': invoice.id})
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)

    def test_remember_name_false_does_not_create_mapping(self):
        """remember_name=False → the name mapping is NOT created."""
        v = _variant()
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='Supplier ABC 123', quantity=1, matched_variant=v)
        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 1, 'remember_name': False}],
        })
        self.assertFalse(
            InvoiceNameMapping.objects.filter(invoice_name='supplier abc 123').exists())

    def test_remember_name_true_creates_mapping(self):
        """remember_name=True → the name mapping is created."""
        v = _variant()
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='Supplier ABC 123', quantity=1, matched_variant=v)
        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 1, 'remember_name': True}],
        })
        m = InvoiceNameMapping.objects.filter(invoice_name='supplier abc 123').first()
        self.assertIsNotNone(m)
        self.assertEqual(m.variant_id, v.id)

    def test_remember_name_omitted_does_not_save(self):
        """The remember_name key is missing (old/out-of-sync frontend) → do NOT save
        (fail-safe; previously the default was True and names silently ended up in the reference
        list)."""
        v = _variant()
        invoice = Invoice.objects.create(photo='invoices/test.jpg')
        it = InvoiceItem.objects.create(
            invoice=invoice, invoice_name='Supplier XYZ', quantity=1, matched_variant=v)
        self._confirm({
            'invoice_id': invoice.id,
            'items': [{'id': it.id, 'variant_id': v.id, 'quantity': 1}],
        })
        self.assertFalse(
            InvoiceNameMapping.objects.filter(invoice_name='supplier xyz').exists())
