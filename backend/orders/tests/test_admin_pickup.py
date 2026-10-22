"""Pickup in the CRM: customer data (name, phone) is optional.

A customer who comes to pick up an order in person often leaves no contacts, and the manager
just needs to create the order. For delivery (Nova Poshta) both fields stay required —
without them there is neither a TTN (waybill) nor a way to contact the recipient.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Category, Product, ProductVariant
from orders.models import Order, OrderItem

User = get_user_model()


class AdminPickupOptionalClientTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email='pickup@test.local', password='x')
        self.client.force_login(self.admin)
        cat, _ = Category.objects.get_or_create(name='Test')
        p = Product.objects.create(name='Plant Tonic, 250 ml')
        p.categories.add(cat)
        self.variant = ProductVariant.objects.create(
            product=p, name='Plant Tonic, 250 ml', price=Decimal('220.00'), stock=10)
        self.url = reverse('admin:orders_order_add')

    def _post(self, **over):
        data = {
            'customer_name': '', 'customer_phone': '', 'customer_email': '',
            'status': 'new', 'delivery_type': 'pickup', 'delivery_address': '',
            'drop_delivery_address': '', 'payment_method': '', 'payment_status': 'pending',
            'total_price': '220.00', 'comment': '', 'manager_comment': '', 'source': '',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '', 'items-0-order': '',
            'items-0-product_variant': self.variant.pk, 'items-0-bundle': '',
            'items-0-quantity': '1', 'items-0-price': '220.00',
            '_save': 'Save',
        }
        data.update(over)
        return self.client.post(self.url, data)

    def test_pickup_saves_without_name_and_phone(self):
        r = self._post()
        self.assertEqual(r.status_code, 302)
        o = Order.objects.get()
        self.assertEqual((o.delivery_type, o.customer_name, o.customer_phone), ('pickup', '', ''))
        self.assertEqual(o.phone_normalized, '')

    def test_pickup_keeps_data_when_given(self):
        r = self._post(customer_name='Jane Doe', customer_phone='+380000000000')
        self.assertEqual(r.status_code, 302)
        o = Order.objects.get()
        self.assertEqual((o.customer_name, o.phone_normalized), ('Jane Doe', '000000000'))

    def test_pickup_paid_in_cash(self):
        """Pickup is most often paid in cash: the method is available in the choices, and stock is written off
immediately."""
        r = self._post(payment_method='cash')
        self.assertEqual(r.status_code, 302)
        o = Order.objects.get()
        self.assertEqual((o.payment_method, o.payment_status), ('cash', 'pending'))
        self.assertEqual(o.get_payment_method_display(), 'Cash')
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 9)

    def test_pickup_at_our_expense(self):
        """«On the house» (gift, exchange): no payment, but stock is written off immediately."""
        r = self._post(payment_method='own_expense')
        self.assertEqual(r.status_code, 302)
        o = Order.objects.get()
        self.assertEqual((o.payment_method, o.payment_status), ('own_expense', 'pending'))
        self.assertEqual(o.get_payment_method_display(), 'On the house')
        self.variant.refresh_from_db()
        self.assertEqual(self.variant.stock, 9)

    def test_export_marks_pickup_order_in_heading(self):
        """In the exported docx, pickup is visible already in the order heading, not only in the delivery line."""
        import io
        import re
        from docx import Document

        def _order(delivery_type, **kw):
            o = Order.objects.create(
                delivery_type=delivery_type, payment_method='cash',
                total_price=Decimal('220.00'), **kw)
            OrderItem.objects.create(
                order=o, product_variant=self.variant, quantity=1, price=Decimal('220.00'))
            return o

        np_order = _order('nova_poshta', customer_name='Jane Doe', customer_phone='+380000000000',
                          delivery_address='Kyiv, branch #1')
        pickup = _order('pickup')
        r = self.client.post('/admin/orders/generate-docx/', {'ids[]': [np_order.pk, pickup.pk]})
        self.assertEqual(r.status_code, 200)
        text = '\n'.join(p.text for p in Document(io.BytesIO(r.content)).paragraphs)

        self.assertRegex(text, r'Order #2[^\n]*PICKUP')
        self.assertNotRegex(text, r'Order #1[^\n]*PICKUP')
        # There is one label: there is no longer a separate «Delivery: PICKUP» line under it.
        self.assertEqual(text.count('PICKUP'), 1)
        self.assertNotIn('Delivery:', text)
        # Without customer data there are no empty «Recipient» / «Phone number» lines — only in the
        # first order.
        self.assertEqual(text.count('Recipient:'), 1)
        self.assertEqual(text.count('Phone number:'), 1)

    def test_nova_poshta_still_requires_name_and_phone(self):
        r = self._post(delivery_type='nova_poshta', delivery_address='Branch #1')
        self.assertEqual(r.status_code, 200)
        form = r.context['adminform'].form
        self.assertIn('customer_name', form.errors)
        self.assertIn('customer_phone', form.errors)
        self.assertEqual(Order.objects.count(), 0)

    def test_nova_poshta_rejects_whitespace_only_name(self):
        r = self._post(delivery_type='nova_poshta', customer_name='   ',
                       customer_phone='+380000000000')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(list(r.context['adminform'].form.errors), ['customer_name'])
        self.assertEqual(Order.objects.count(), 0)
