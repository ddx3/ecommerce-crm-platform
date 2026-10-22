"""Bundles no longer have attributes (Bundle.attribute_values removed): no mention of them is left
anywhere in the API, admin or CRM list, and the pages don't crash."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.test import RequestFactory, TestCase, override_settings

from catalog.models import AttributeValue, Bundle, BundleItem, Product, ProductVariant


@override_settings(ALLOWED_HOSTS=['testserver'])
class BundleWithoutAttributesTests(TestCase):
    def setUp(self):
        product = Product.objects.create(name='Plant Booster')
        self.variant = ProductVariant.objects.create(product=product, name='Plant Booster, 25 ml', price=Decimal('100'), stock=5)
        self.bundle = Bundle.objects.create(name='Bundle "Test"', is_active=True)
        BundleItem.objects.create(bundle=self.bundle, product_variant=self.variant, quantity=1)
        self.client.force_login(get_user_model().objects.create(email='attrs@example.com', is_staff=True, is_superuser=True))

    def test_model_has_no_attribute_values(self):
        with self.assertRaises(FieldDoesNotExist):
            Bundle._meta.get_field('attribute_values')
        self.assertFalse(hasattr(AttributeValue, 'bundles'))

    def test_public_api_has_no_attribute_values(self):
        listing = self.client.get('/api/bundles/')
        self.assertEqual(listing.status_code, 200)
        rows = listing.json()['results']
        self.assertEqual([r['id'] for r in rows], [self.bundle.pk])
        self.assertNotIn('attribute_values', rows[0])
        detail = self.client.get(f'/api/bundles/{self.bundle.slug}/')
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('attribute_values', detail.json())

    def test_admin_forms_render_without_attribute_widget(self):
        for url in ('/admin/catalog/bundle/add/', f'/admin/catalog/bundle/{self.bundle.pk}/change/'):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            html = resp.content.decode()
            self.assertNotIn('attribute_values', html, url)
            self.assertNotIn('attr-widget-root', html, url)
            self.assertNotIn('initAttrWidget', html, url)

    def test_saving_new_and_existing_bundle_through_admin_still_works(self):
        import json
        resp = self.client.post('/admin/catalog/bundle/add/', {
            'name': 'Bundle "New"', 'is_active': 'on', 'sort_order': '0',
            '_bundle_items': json.dumps([{'variant_id': self.variant.pk, 'qty': 2}]),
            '_save': 'Save',
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, 'context', None) and resp.context['adminform'].form.errors)
        created = Bundle.objects.get(name='Bundle "New"')
        self.assertEqual([(i.product_variant_id, i.quantity) for i in created.items.all()], [(self.variant.pk, 2)])
        resp = self.client.post(f'/admin/catalog/bundle/{created.pk}/change/', {
            'name': 'Bundle "New 2"', 'is_active': 'on', 'sort_order': '0', '_save': 'Save',
        })
        self.assertEqual(resp.status_code, 302)
        created.refresh_from_db()
        self.assertEqual(created.name, 'Bundle "New 2"')

    def test_crm_list_data_has_no_attrs(self):
        resp = self.client.get('/admin/catalog/bundles-data/')
        self.assertEqual(resp.status_code, 200)
        row = resp.json()[0]
        self.assertEqual(row['bundle_id'], self.bundle.pk)
        self.assertNotIn('attrs', row)

    def test_course_related_bundles_still_serialize(self):
        from courses.models import Course
        from courses.serializers import CourseDetailSerializer
        course = Course.objects.create(name='Course', slug='course', price=Decimal('0'))
        course.related_bundles.add(self.bundle)
        request = RequestFactory().get('/')
        data = CourseDetailSerializer(course, context={'request': request}).data
        self.assertEqual([b['id'] for b in data['related_bundles']], [self.bundle.pk])
        self.assertNotIn('attribute_values', data['related_bundles'][0])
