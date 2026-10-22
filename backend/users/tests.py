from django.test import TestCase

from users.models import User


class UserAdminSearchTests(TestCase):
    """Custom User without username: the search_fields inherited from Django with 'username' caused a
FieldError (500)."""

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_superuser(email='owner@example.com', password='x')
        User.objects.create_user(email='jane@example.com', password='x', first_name='Jane', phone='+380000000000')

    def setUp(self):
        self.client.force_login(self.owner)

    def test_changelist_search_does_not_crash(self):
        for q in ('jane', 'Jane', '0000000000'):
            resp = self.client.get('/admin/users/user/', {'q': q}, secure=True)
            self.assertEqual(resp.status_code, 200, q)

    def test_user_autocomplete_on_course_purchase_form_works(self):
        resp = self.client.get('/admin/autocomplete/', {
            'term': 'jane', 'app_label': 'courses', 'model_name': 'coursepurchase', 'field_name': 'user',
        }, secure=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('jane@example.com', resp.content.decode())

    def test_search_fields_only_name_real_columns(self):
        from django.contrib import admin
        names = {f.name for f in User._meta.get_fields()}
        for field in admin.site._registry[User].search_fields:
            self.assertIn(field.split('__')[0], names)
