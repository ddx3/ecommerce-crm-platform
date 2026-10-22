"""Tests for access to the course's paid video.

The main thing checked here: the video cannot be taken without a purchase. Before the fix
nginx served `/media/courses/lessons/*.mp4` as ordinary static files, so the paywall
was only on the JSON API — see courses/video_access.py.
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.core import signing
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from courses import video_access
from courses.models import Course, CourseModule, CourseLesson, CoursePurchase
from orders.models import Order, OrderItem

User = get_user_model()


# We disable X-Accel: there is no nginx in tests, we want the real bytes in the response.
@override_settings(COURSES_X_ACCEL=False)
class CourseVideoAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name='Test Course', price=Decimal('888.00'))
        cls.course.full_video_file.save(
            'full.mp4', SimpleUploadedFile('full.mp4', b'FULLVIDEOBYTES'), save=True)
        module = CourseModule.objects.create(course=cls.course, title='Module')
        cls.paid = CourseLesson.objects.create(module=module, title='Paid', is_preview=False)
        cls.paid.video_file.save(
            'paid.mp4', SimpleUploadedFile('paid.mp4', b'PAIDVIDEOBYTES'), save=True)
        cls.free = CourseLesson.objects.create(module=module, title='Preview', is_preview=True)
        cls.free.video_file.save(
            'free.mp4', SimpleUploadedFile('free.mp4', b'FREEVIDEOBYTES'), save=True)

        cls.buyer = User.objects.create_user(email='buyer@test.com', password='x')
        cls.stranger = User.objects.create_user(email='stranger@test.com', password='x')
        CoursePurchase.objects.create(user=cls.buyer, course=cls.course)

    def _get(self, kind, obj_id, user_id):
        token = video_access.sign(kind, obj_id, user_id)
        return self.client.get(f'/api/courses/video/{token}/')

    def test_buyer_gets_paid_lesson_video(self):
        r = self._get(video_access.KIND_LESSON, self.paid.id, self.buyer.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'PAIDVIDEOBYTES')

    def test_buyer_gets_full_video(self):
        r = self._get(video_access.KIND_FULL, self.course.id, self.buyer.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'FULLVIDEOBYTES')

    def test_anonymous_token_cannot_open_paid_lesson(self):
        r = self._get(video_access.KIND_LESSON, self.paid.id, None)
        self.assertEqual(r.status_code, 403)

    def test_non_buyer_cannot_open_paid_lesson(self):
        r = self._get(video_access.KIND_LESSON, self.paid.id, self.stranger.id)
        self.assertEqual(r.status_code, 403)

    def test_non_buyer_cannot_open_full_video(self):
        r = self._get(video_access.KIND_FULL, self.course.id, self.stranger.id)
        self.assertEqual(r.status_code, 403)

    def test_preview_lesson_is_public(self):
        r = self._get(video_access.KIND_LESSON, self.free.id, None)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content), b'FREEVIDEOBYTES')

    def test_tampered_token_rejected(self):
        token = video_access.sign(video_access.KIND_LESSON, self.paid.id, self.stranger.id)
        # Substituting user_id in the payload without re-signing — a classic bypass attempt.
        forged = token[:-3] + ('aaa' if not token.endswith('aaa') else 'bbb')
        r = self.client.get(f'/api/courses/video/{forged}/')
        self.assertEqual(r.status_code, 403)

    def test_expired_token_rejected(self):
        token = video_access.sign(video_access.KIND_LESSON, self.paid.id, self.buyer.id)
        # max_age=-1 — the token 'expired' immediately; a real one behaves the same via TTL.
        with self.assertRaises(signing.SignatureExpired):
            signing.loads(token, salt=video_access.SALT, max_age=-1)
        with patch.object(video_access, 'TTL_SECONDS', -1):
            r = self.client.get(f'/api/courses/video/{token}/')
        self.assertEqual(r.status_code, 410)

    def test_revoked_purchase_kills_existing_link(self):
        """The token was already issued but the purchase was rolled back — the link must die."""
        token = video_access.sign(video_access.KIND_LESSON, self.paid.id, self.buyer.id)
        CoursePurchase.objects.filter(user=self.buyer, course=self.course).delete()
        r = self.client.get(f'/api/courses/video/{token}/')
        self.assertEqual(r.status_code, 403)

    def test_api_never_leaks_raw_media_path(self):
        """Neither the course detail nor the lesson content return /media/courses/lessons/."""
        # DRF here is JWT-only (SessionAuthentication is not wired) — force_login will not do.
        access = str(RefreshToken.for_user(self.buyer).access_token)
        self.client.defaults['HTTP_AUTHORIZATION'] = f'Bearer {access}'
        detail = self.client.get(f'/api/courses/{self.course.slug}/')
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('/media/courses/lessons/', detail.content.decode())
        self.assertIn('/api/courses/video/', detail.json()['full_video_url'])

        lesson = self.client.get(f'/api/courses/{self.course.slug}/lessons/{self.paid.id}/')
        self.assertEqual(lesson.status_code, 200)
        self.assertNotIn('/media/courses/lessons/', lesson.content.decode())
        self.assertIn('/api/courses/video/', lesson.json()['video_file_url'])


class HiddenCourseStillOwnedTests(TestCase):
    """is_active=False hides the course from the storefront but does not take it away from the buyer."""

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name='Hidden', price=Decimal('500.00'), is_active=False)
        module = CourseModule.objects.create(course=cls.course, title='M')
        cls.lesson = CourseLesson.objects.create(module=module, title='L', is_preview=False)
        cls.buyer = User.objects.create_user(email='b2@test.com', password='x')
        cls.outsider = User.objects.create_user(email='o2@test.com', password='x')
        CoursePurchase.objects.create(user=cls.buyer, course=cls.course)

    def _as(self, user):
        token = str(RefreshToken.for_user(user).access_token)
        return {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    def test_buyer_still_opens_hidden_course(self):
        r = self.client.get(f'/api/courses/{self.course.slug}/', **self._as(self.buyer))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['has_access'])

    def test_buyer_still_opens_hidden_course_lesson(self):
        r = self.client.get(
            f'/api/courses/{self.course.slug}/lessons/{self.lesson.id}/', **self._as(self.buyer))
        self.assertEqual(r.status_code, 200)

    def test_hidden_course_is_404_for_everyone_else(self):
        self.assertEqual(self.client.get(f'/api/courses/{self.course.slug}/').status_code, 404)
        r = self.client.get(f'/api/courses/{self.course.slug}/', **self._as(self.outsider))
        self.assertEqual(r.status_code, 404)

    def test_hidden_course_absent_from_catalog(self):
        r = self.client.get('/api/courses/')
        self.assertNotIn(self.course.slug, r.content.decode())

    def test_hidden_course_still_listed_in_my_courses(self):
        r = self.client.get('/api/me/courses/', **self._as(self.buyer))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            [p['course']['slug'] for p in r.json()['purchased']], [self.course.slug])


class SetStatusGrantsAccessTests(TestCase):
    """The CRM "Payments" section sets payment_status via queryset.update() — post_save does not
    fire there, so granting the course must be mirrored manually."""

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name='Paid Course', price=Decimal('499.00'))
        cls.buyer = User.objects.create_user(email='bt@test.com', password='x')
        cls.staff = User.objects.create_user(email='staff@test.com', password='x', is_staff=True)
        cls.staff.is_superuser = True
        cls.staff.save(update_fields=['is_superuser'])

    def _order(self):
        order = Order.objects.create(
            user=self.buyer, customer_name='Jane Doe', customer_phone='+380000000000',
            customer_email='bt@test.com', delivery_type='nova_poshta',
            delivery_address='— digital item —', payment_method='bank_transfer',
            payment_status='pending', total_price=Decimal('499.00'),
        )
        OrderItem.objects.create(order=order, course=self.course, quantity=1,
                                 price=Decimal('499.00'))
        return order

    def test_set_status_paid_grants_course(self):
        order = self._order()
        self.assertFalse(CoursePurchase.objects.filter(user=self.buyer).exists())

        self.client.force_login(self.staff)
        r = self.client.post(
            '/admin/notifications/payment-action/',
            data=json.dumps({'action': 'set_status', 'status': 'paid', 'id': order.id}),
            content_type='application/json',
        )
        self.assertEqual(r.status_code, 200, r.content)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertTrue(
            CoursePurchase.objects.filter(user=self.buyer, course=self.course).exists(),
            'set_status marked the order as paid but did not grant access to the course',
        )

    def test_set_status_pending_revokes_course(self):
        """The manager removed the payment — access to the course must disappear with it."""
        order = self._order()
        self.client.force_login(self.staff)
        for status_value in ('paid', 'pending'):
            r = self.client.post(
                '/admin/notifications/payment-action/',
                data=json.dumps({'action': 'set_status', 'status': status_value, 'id': order.id}),
                content_type='application/json',
            )
            self.assertEqual(r.status_code, 200, r.content)

        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'pending')
        self.assertFalse(
            CoursePurchase.objects.filter(user=self.buyer, course=self.course).exists(),
            'the payment was rolled back but the access to the course remained',
        )

    def test_grant_is_idempotent(self):
        order = self._order()
        from courses.signals import grant_course_access
        order.payment_status = 'paid'
        self.assertEqual(grant_course_access(order), 1)
        self.assertEqual(grant_course_access(order), 0)
        self.assertEqual(CoursePurchase.objects.filter(user=self.buyer).count(), 1)

    def test_guest_order_grants_nothing(self):
        order = self._order()
        order.user = None
        order.save(update_fields=['user'])
        from courses.signals import grant_course_access
        self.assertEqual(grant_course_access(order), 0)


@override_settings(COURSES_X_ACCEL=True)
class XAccelHandoffTests(TestCase):
    """On prod nginx serves the bytes — Django should return only the redirect header."""

    @classmethod
    def setUpTestData(cls):
        cls.course = Course.objects.create(name='X-Accel Course', price=Decimal('100.00'))
        module = CourseModule.objects.create(course=cls.course, title='M')
        cls.lesson = CourseLesson.objects.create(module=module, title='L', is_preview=True)
        cls.lesson.video_file.save(
            'xa.mp4', SimpleUploadedFile('xa.mp4', b'BYTES'), save=True)

    def test_sends_internal_redirect_not_body(self):
        token = video_access.sign(video_access.KIND_LESSON, self.lesson.id, None)
        r = self.client.get(f'/api/courses/video/{token}/')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r['X-Accel-Redirect'].startswith('/protected-media/'))
        self.assertIn(self.lesson.video_file.name, r['X-Accel-Redirect'])
        self.assertEqual(r.content, b'')
        self.assertEqual(r['Content-Type'], 'video/mp4')


class CoursePurchaseAdminTests(TestCase):
    """Course purchase form: the "Order item" field is raw_id, not a <select> over all
    OrderItem.

    In production (≈55,000 items) a regular select made one query per option: the page took ~1 min
    to open and weighed 5 MB."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser(email='owner@example.com', password='x')
        cls.course = Course.objects.create(name='Admin Course', price=Decimal('100.00'))
        cls.purchase = CoursePurchase.objects.create(user=cls.owner, course=cls.course)

    def test_order_item_is_a_raw_id_input_not_a_select(self):
        self.client.force_login(self.owner)
        resp = self.client.get(f'/admin/courses/coursepurchase/{self.purchase.pk}/change/', secure=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn('<select name="order_item"', html)
        self.assertIn('<input type="text" name="order_item"', html)
