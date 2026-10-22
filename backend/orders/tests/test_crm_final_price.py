"""Item price in a manual CRM order — with the discount (variant/brand), as on the storefront.

History: in prod, the 10% brand discount for «Living Earth» applied to orders from the
site
(create_order), but in manual CRM orders (insta/viber) items were entered at the BASE price —
product search returned only v.price, and the order form put it into the price field.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from catalog.models import Brand, Category, Product, ProductVariant
from orders.models import Order

User = get_user_model()


def _variant(price, brand=None, **kw):
    cat, _ = Category.objects.get_or_create(name='Test')
    p = Product.objects.create(name='Compost Activator', brand=brand)
    p.categories.add(cat)
    return ProductVariant.objects.create(product=p, name='Compost Activator, 15 g', price=Decimal(price), stock=10, **kw)


class CrmFinalPriceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email='fp@test.local', password='x')
        self.client.force_login(self.admin)
        self.brand = Brand.objects.create(name='Living Earth', discount_percent=10)

    def test_final_price_whole_applies_brand_discount(self):
        # 49 * 0.9 = 44.10 → 44 (like _money_whole in create_order)
        self.assertEqual(_variant('49.00', self.brand).final_price_whole, Decimal('44'))
        # 24 * 0.9 = 21.60 → 22
        self.assertEqual(_variant('24.00', self.brand).final_price_whole, Decimal('22'))

    def test_own_variant_discount_beats_brand(self):
        v = _variant('100.00', self.brand, discount_percent=Decimal('50'))
        self.assertEqual(v.final_price_whole, Decimal('50'))

    def test_no_discount_keeps_base(self):
        self.assertEqual(_variant('93.00').final_price_whole, Decimal('93'))

    def test_variant_search_returns_final_price_and_keeps_base(self):
        v = _variant('49.00', self.brand)
        data = self.client.get('/admin/catalog/variant-search/', {'q': 'Activator'}).json()
        row = next(r for r in data['results'] if r['id'] == v.pk)
        self.assertEqual(row['price'], '49.00')        # the bundle editor needs the base price
        self.assertEqual(row['final_price'], '44')

    def test_variant_details_returns_final_price(self):
        v = _variant('250.00', self.brand)
        data = self.client.get('/admin/catalog/variant-details/', {'ids': str(v.pk)}).json()
        self.assertEqual(data['variants'][str(v.pk)]['final_price'], '225')

    def test_orders_search_variants_returns_final_price(self):
        v = _variant('49.00', self.brand)
        data = self.client.get('/admin/orders/invoice/search-variants/', {'q': 'Activator'}).json()
        row = next(r for r in data['results'] if r['id'] == v.pk)
        self.assertEqual(row['final_price'], '44')

    def test_admin_empty_price_falls_back_to_final_price(self):
        v = _variant('49.00', self.brand)
        data = {
            'customer_name': 'Jane Doe', 'customer_phone': '+380000000000',
            'customer_email': '', 'status': 'new',
            'delivery_type': 'nova_poshta', 'delivery_address': 'Branch #1',
            'drop_delivery_address': '', 'payment_method': 'bank_transfer',
            'payment_status': 'pending', 'total_price': '0', 'comment': '',
            'manager_comment': '', 'source': '',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'items-0-id': '', 'items-0-order': '', 'items-0-product_variant': v.pk,
            'items-0-bundle': '', 'items-0-quantity': '1', 'items-0-price': '',
            '_save': 'Save',
        }
        r = self.client.post(reverse('admin:orders_order_add'), data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Order.objects.get().items.get().price, Decimal('44'))
