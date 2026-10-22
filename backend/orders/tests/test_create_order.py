"""Tests of critical paths: checkout, stock, Monobank webhook,
bundle discounts. No live external APIs — the Monobank signature is mocked."""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from catalog.models import (
    Category, Product, ProductVariant, Bundle, BundleItem,
    Label, VariantLabel,
)
from courses.models import Course, CoursePurchase
from orders.models import Order, OrderItem, OrderNumberCounter

User = get_user_model()


def _variant(stock=10, price='100.00'):
    cat = Category.objects.create(name='Test')
    p = Product.objects.create(name='Test product')
    p.categories.add(cat)
    return ProductVariant.objects.create(product=p, price=Decimal(price), stock=stock)


def _order_payload(items, payment_method='cod'):
    return {
        'customer_name': 'Jane Doe',
        'customer_phone': '+380000000000',
        'delivery_type': 'nova_poshta',
        'delivery_address': 'Kyiv, branch 1',
        'payment_method': payment_method,
        'items': items,
    }


@override_settings(ANTIBOT_ENABLED=False)
class CreateOrderTests(TestCase):
    def _post(self, payload, token=None):
        headers = {}
        if token:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {token}'
        return self.client.post(
            '/api/orders/create/', data=json.dumps(payload),
            content_type='application/json', **headers,
        )

    def test_order_consumes_stock_and_computes_total(self):
        v = _variant(stock=10, price='100.00')
        resp = self._post(_order_payload([{'variant_id': v.id, 'qty': 3}]))
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(order_number=resp.json()['order_number'])
        self.assertEqual(order.total_price, Decimal('300.00'))
        v.refresh_from_db()
        self.assertEqual(v.stock, 7)

    def test_order_rejected_when_out_of_stock(self):
        v = _variant(stock=2)
        resp = self._post(_order_payload([{'variant_id': v.id, 'qty': 5}]))
        self.assertEqual(resp.status_code, 400)
        v.refresh_from_db()
        self.assertEqual(v.stock, 2)  # stock unchanged

    def test_guest_cannot_buy_course(self):
        course = Course.objects.create(name='Course', price=Decimal('500'), is_active=True)
        resp = self._post(_order_payload(
            [{'is_course': True, 'variant_id': course.id}]
        ))
        self.assertEqual(resp.status_code, 401)

    def test_repeat_course_purchase_blocked(self):
        course = Course.objects.create(name='Course', price=Decimal('500'), is_active=True)
        user = User.objects.create_user(email='u@test.com', password='pass12345')
        CoursePurchase.objects.create(user=user, course=course)
        token = str(RefreshToken.for_user(user).access_token)
        resp = self._post(
            _order_payload([{'is_course': True, 'variant_id': course.id}]),
            token=token,
        )
        self.assertEqual(resp.status_code, 400)


class MonobankWebhookTests(TestCase):
    def _order(self, invoice_id='inv-1', total='150.00'):
        v = _variant(price=total)
        order = Order.objects.create(
            customer_name='J', customer_phone='+380000000001',
            delivery_type='nova_poshta', delivery_address='Kyiv',
            payment_method='monobank', total_price=Decimal(total),
            monobank_invoice_id=invoice_id,
        )
        OrderItem.objects.create(order=order, product_variant=v, quantity=1, price=Decimal(total))
        return order

    def _webhook(self, payload):
        return self.client.post(
            '/api/monobank/webhook/', data=json.dumps(payload),
            content_type='application/json', HTTP_X_SIGN='fake',
        )

    @patch('orders.monobank.verify_signature', return_value=True)
    def test_success_marks_paid(self, _mock):
        order = self._order(invoice_id='inv-ok', total='150.00')
        resp = self._webhook({
            'invoiceId': 'inv-ok', 'status': 'success',
            'ccy': 980, 'amount': 15000,
        })
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')

    @patch('orders.monobank.verify_signature', return_value=True)
    def test_amount_mismatch_rejected(self, _mock):
        order = self._order(invoice_id='inv-bad', total='150.00')
        resp = self._webhook({
            'invoiceId': 'inv-bad', 'status': 'success',
            'ccy': 980, 'amount': 99999,
        })
        self.assertEqual(resp.status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')

    @patch('orders.monobank.verify_signature', return_value=True)
    def test_failure_keeps_pending(self, _mock):
        # Binary payment status: a failure does not set a separate 'failed',
        # the order stays 'pending' (the customer can try again).
        order = self._order(invoice_id='inv-fail', total='150.00')
        resp = self._webhook({
            'invoiceId': 'inv-fail', 'status': 'failure',
            'ccy': 980, 'amount': 15000,
        })
        self.assertEqual(resp.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')

    @patch('orders.monobank.verify_signature', return_value=False)
    def test_invalid_signature_rejected(self, _mock):
        resp = self._webhook({'invoiceId': 'x', 'status': 'success'})
        self.assertEqual(resp.status_code, 400)


class BundleDiscountTests(TestCase):
    def test_bundle_own_discount_applied(self):
        bundle = Bundle.objects.create(
            name='Bundle', price=Decimal('1000'),
            discount_percent=Decimal('20'),
        )
        self.assertEqual(bundle.get_price(), Decimal('800.00'))
        self.assertEqual(bundle.get_base_price(), Decimal('1000'))
        self.assertEqual(bundle.get_old_price(), Decimal('1000'))

    def test_component_discount_flows_into_bundle(self):
        bundle = Bundle.objects.create(name='Bundle2')
        v = _variant(price='200.00')
        v.discount_percent = Decimal('50')
        v.save()
        BundleItem.objects.create(bundle=bundle, product_variant=v, quantity=2)
        # 2 pcs at a base price of 200 = 400; with a 50% discount = 200
        self.assertEqual(bundle.get_base_price(), Decimal('400.00'))
        self.assertEqual(bundle.get_price(), Decimal('200.00'))
        self.assertEqual(bundle.get_old_price(), Decimal('400.00'))

    def test_no_discount_returns_none_old_price(self):
        bundle = Bundle.objects.create(name='Bundle3', price=Decimal('500'))
        self.assertEqual(bundle.get_price(), Decimal('500.00'))
        self.assertIsNone(bundle.get_old_price())


@override_settings(ANTIBOT_ENABLED=False)
class HybridPidZamovlennyaLabelTests(TestCase):
    """Regression: the "made to order" label of a 50/50 variant WITHOUT a bulk bag must
    change automatically
    when stock is written off/returned via an order (stock there is changed by queryset
    .update()/F() bypassing the post_save signal — so we call the recompute explicitly)."""

    def setUp(self):
        self.pid, _ = Label.objects.get_or_create(
            slug=Label.SLUG_PID_ZAMOVLENNYA, defaults={'name': 'Made to order'},
        )

    def _hybrid(self, stock):
        cat = Category.objects.create(name='Test')
        p = Product.objects.create(name='Hybrid')
        p.categories.add(cat)
        return ProductVariant.objects.create(
            product=p, price=Decimal('300.00'), stock=stock, is_hybrid=True,
        )

    def _has_pid(self, v):
        return VariantLabel.objects.filter(variant=v, label=self.pid).exists()

    def _create_order(self, variant, qty):
        resp = self.client.post(
            '/api/orders/create/',
            data=json.dumps(_order_payload([{'variant_id': variant.id, 'qty': qty}])),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return Order.objects.get(order_number=resp.json()['order_number'])

    def test_selling_last_unit_adds_pid_label(self):
        v = self._hybrid(stock=1)
        self.assertFalse(self._has_pid(v))  # stock>0 → no label
        self._create_order(v, qty=1)
        v.refresh_from_db()
        self.assertEqual(v.stock, 0)
        self.assertTrue(self._has_pid(v))  # stock exhausted → "made to order" label appeared

    def test_cancelling_order_removes_pid_label(self):
        v = self._hybrid(stock=1)
        order = self._create_order(v, qty=1)
        v.refresh_from_db()
        self.assertTrue(self._has_pid(v))
        order.status = 'cancelled'
        order.save()
        v.refresh_from_db()
        self.assertEqual(v.stock, 1)
        self.assertFalse(self._has_pid(v))  # stock returned → label removed


@override_settings(ANTIBOT_ENABLED=False)
class OrderNumberCounterTests(TransactionTestCase):
    """Regression: order_number must not 'burn out' with gaps on rollback of a failed checkout
    (the number was taken from pk, and a Postgres sequence is not rolled back together with the
    transaction).
    TransactionTestCase — so that transaction.on_commit() in save() actually fires
    (in a regular TestCase the request sits in a savepoint, not in a real commit)."""

    def _post(self, payload):
        return self.client.post(
            '/api/orders/create/', data=json.dumps(payload), content_type='application/json',
        )

    def test_failed_order_does_not_burn_a_number(self):
        v = _variant(stock=1, price='300.00')
        before = OrderNumberCounter.objects.get_or_create(pk=1)[0].last_number

        # Failed attempt: qty exceeds stock level → rollback ALREADY AFTER Order.objects.create().
        resp = self._post(_order_payload([{'variant_id': v.id, 'qty': 5}]))
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(OrderNumberCounter.objects.get(pk=1).last_number, before)

        # Successful checkout right after — the number continues the counter with no gap.
        resp = self._post(_order_payload([{'variant_id': v.id, 'qty': 1}]))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['order_number'], str(before + 1))


@override_settings(ANTIBOT_ENABLED=False)
class PickupDeliveryTests(TestCase):
    """Pickup: no delivery address, no 'own' Nova Poshta parcel is created."""

    def _post(self, payload):
        return self.client.post(
            '/api/orders/create/', data=json.dumps(payload),
            content_type='application/json',
        )

    def test_pickup_order_created_without_address(self):
        v = _variant(stock=5)
        payload = _order_payload([{'variant_id': v.id, 'qty': 3}])
        payload['delivery_type'] = 'pickup'
        payload.pop('delivery_address')
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.content)
        order = Order.objects.get(order_number=resp.json()['order_number'])
        self.assertEqual(order.delivery_type, 'pickup')
        self.assertEqual(order.delivery_address, '')

    def test_pickup_order_has_no_own_shipment(self):
        v = _variant(stock=5)
        payload = _order_payload([{'variant_id': v.id, 'qty': 3}])
        payload['delivery_type'] = 'pickup'
        payload.pop('delivery_address')
        order = Order.objects.get(order_number=self._post(payload).json()['order_number'])
        self.assertFalse(order.shipments.filter(kind='own').exists())

    def test_nova_poshta_still_requires_address(self):
        # Relaxing required must not extend to carrier delivery.
        v = _variant(stock=5)
        payload = _order_payload([{'variant_id': v.id, 'qty': 3}])
        payload.pop('delivery_address')
        self.assertEqual(self._post(payload).status_code, 400)

    def test_switching_to_pickup_drops_own_shipment(self):
        # The manager switches the order from Nova Poshta → pickup in the CRM: the parcel must
        # disappear.
        from orders.models import recompute_shipments
        v = _variant(stock=5)
        order = Order.objects.get(
            order_number=self._post(_order_payload([{'variant_id': v.id, 'qty': 3}])).json()['order_number']
        )
        self.assertTrue(order.shipments.filter(kind='own').exists())
        order.delivery_type = 'pickup'
        order.delivery_address = ''
        order.save()
        recompute_shipments(order)
        self.assertFalse(order.shipments.filter(kind='own').exists())


@override_settings(ANTIBOT_ENABLED=False)
class EmptyPayloadTests(TestCase):
    def test_empty_json_object_is_a_400_not_a_500(self):
        """`{}`: DRF has already read the stream, and the fallback `request.body` raised RawPostDataException →
500 instead of 400."""
        resp = self.client.post('/api/orders/create/', data='{}', content_type='application/json')
        self.assertEqual(resp.status_code, 400, resp.content)
