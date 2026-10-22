"""The same product cannot become two order items (regression for 12735).

History: in the CRM a manager picked the same product three times in the "Add product…"
field,
removed the extra rows with ✕ — and all three items were still saved (✕ ticked the DELETE checkbox,
which admin does not render for NEW rows). Stock was written off twice, and the order total
stayed in line with the visible table.

This tests the server side: a duplicate is rejected by formset validation, and a
"cleared" row (what ✕ now does for a new item) is not saved.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Category, Product, ProductVariant
from orders.models import Order

User = get_user_model()


def _variant(name='Orchid Feed, 250 ml', price='220.00', stock=10):
    cat, _ = Category.objects.get_or_create(name='Test')
    p = Product.objects.create(name=name)
    p.categories.add(cat)
    return ProductVariant.objects.create(product=p, name=name, price=Decimal(price), stock=stock)


class AdminOrderItemDuplicateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email='dup@test.local', password='x')
        self.client.force_login(self.admin)
        self.v1 = _variant('Orchid Feed, 250 ml', '220.00', stock=10)
        self.v2 = _variant('Rose Spray, 250 ml', '140.00', stock=10)
        self.url = reverse('admin:orders_order_add')

    def _post(self, rows, total='0'):
        data = {
            'customer_name': 'Jane Doe', 'customer_phone': '+380000000000',
            'customer_email': '', 'status': 'new',
            'delivery_type': 'nova_poshta', 'delivery_address': 'Branch No. 1',
            'drop_delivery_address': '', 'payment_method': 'bank_transfer',
            'payment_status': 'pending', 'total_price': total, 'comment': '',
            'manager_comment': '', 'source': '',
            'items-TOTAL_FORMS': str(len(rows)), 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            '_save': 'Save',
        }
        for i, r in enumerate(rows):
            data['items-%d-id' % i] = ''
            data['items-%d-order' % i] = ''
            data['items-%d-product_variant' % i] = r.get('variant', '')
            data['items-%d-bundle' % i] = r.get('bundle', '')
            data['items-%d-quantity' % i] = r.get('qty', '')
            data['items-%d-price' % i] = r.get('price', '')
        return self.client.post(self.url, data)

    def test_same_variant_twice_rejected(self):
        r = self._post([
            {'variant': self.v1.pk, 'qty': '1', 'price': '220.00'},
            {'variant': self.v1.pk, 'qty': '1', 'price': '220.00'},
        ], total='220.00')
        self.assertEqual(r.status_code, 200)          # the form came back with an error, no redirect
        self.assertContains(r, 'appears in the order twice')
        self.assertEqual(Order.objects.count(), 0)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.stock, 10)           # stock untouched

    def test_cleared_row_is_not_saved(self):
        """This is how ✕ works now on a freshly added row: the fields are empty → Django skips it."""
        r = self._post([
            {'variant': self.v1.pk, 'qty': '1', 'price': '220.00'},
            {'variant': '', 'qty': '', 'price': ''},
            {'variant': self.v2.pk, 'qty': '2', 'price': '140.00'},
        ], total='500.00')
        self.assertEqual(r.status_code, 302)
        o = Order.objects.get()
        self.assertEqual(
            sorted(o.items.values_list('product_variant_id', 'quantity')),
            sorted([(self.v1.pk, 1), (self.v2.pk, 2)]))
        self.v1.refresh_from_db()
        self.v2.refresh_from_db()
        self.assertEqual((self.v1.stock, self.v2.stock), (9, 8))

    def test_different_variants_ok(self):
        r = self._post([
            {'variant': self.v1.pk, 'qty': '1', 'price': '220.00'},
            {'variant': self.v2.pk, 'qty': '1', 'price': '140.00'},
        ], total='360.00')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Order.objects.get().items.count(), 2)
