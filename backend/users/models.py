from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('Email is required')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('role', 'admin')
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    ROLE_CHOICES = [
        ('client', 'Customer'),
        ('admin', 'Administrator'),
    ]

    # Admin sub-role. Filled in only for role='admin'.
    #   superuser - full access (Django is_superuser=True).
    #   manager   - changes everything except other admins and site settings.
    #   viewer    - view only (read-only across the whole CRM).
    ADMIN_ROLE_CHOICES = [
        ('superuser', 'Chief admin'),
        ('manager',   'Manager'),
        ('viewer',    'Viewer'),
    ]

    GENDER_CHOICES = [
        ('female', 'Female'),
        ('male', 'Male'),
        ('na', 'Prefer not to say'),
    ]

    REFERRAL_SOURCE_CHOICES = [
        ('friends', 'From friends'),
        ('social',  'Social networks'),
        ('google',  'Google / search'),
        ('youtube', 'YouTube'),
        ('ad',      'Advertising'),
        ('other',   'Other'),
    ]

    REFERRAL_SOCIAL_CHOICES = [
        ('instagram', 'Instagram'),
        ('facebook',  'Facebook'),
        ('tiktok',    'TikTok'),
        ('other',     'Other'),
    ]

    email = models.EmailField(unique=True, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, verbose_name='Phone')
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='client')
    admin_role = models.CharField(
        max_length=10, choices=ADMIN_ROLE_CHOICES, blank=True, default='',
        verbose_name='Admin role',
        help_text='Filled in only for admins. Defines what the user can do in the CRM.',
    )
    np_city = models.CharField(max_length=100, blank=True, verbose_name='Nova Poshta: city')
    np_city_ref = models.CharField(max_length=100, blank=True, verbose_name='Nova Poshta: city ref')
    np_warehouse = models.CharField(max_length=255, blank=True, verbose_name='Nova Poshta: branch')

    # ─── Questionnaire (all fields optional) ───
    # We store the keys of the choices fields. UI labels are in
    # frontend/src/constants/questionnaire.ts.
    gender = models.CharField(max_length=8, choices=GENDER_CHOICES, blank=True, verbose_name='Gender')
    birth_date = models.DateField(null=True, blank=True, verbose_name='Birth date',
                                  help_text='For birthday greetings and discounts')
    gardening_text = models.TextField(blank=True, verbose_name='What they grow',
                                      help_text='Free-form description, later processed by an LLM for recommendations')
    interests = models.JSONField(default=list, blank=True, verbose_name='Categories of interest',
                                 help_text='Keys of the product categories the customer is interested in')
    referral_source = models.CharField(max_length=10, choices=REFERRAL_SOURCE_CHOICES, blank=True,
                                       verbose_name='How they heard about the shop')
    referral_social = models.CharField(max_length=10, choices=REFERRAL_SOCIAL_CHOICES, blank=True,
                                       verbose_name='Social network (if source=social)')
    referral_other = models.CharField(max_length=200, blank=True,
                                      verbose_name='Source description (if source=other or social=other)')
    wishes = models.TextField(blank=True, verbose_name='Wishes',
                              help_text='What the customer would like to see in the shop')
    questionnaire_dismissed_at = models.DateTimeField(null=True, blank=True, verbose_name='Questionnaire: "do not show"')
    questionnaire_remind_after = models.DateTimeField(null=True, blank=True, verbose_name='Questionnaire: "remind after"')

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return self.email or self.phone or str(self.pk)
