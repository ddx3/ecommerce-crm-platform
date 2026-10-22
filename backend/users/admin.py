import json
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import Group
from django.utils import timezone
from core.admin_filters import IsActiveFilter
from core.jsonutils import js_json
from .models import User

admin.site.unregister(Group)

# Hide the service JWT blacklist tables from the admin; they are maintained automatically.
try:
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
    admin.site.unregister(BlacklistedToken)
    admin.site.unregister(OutstandingToken)
except Exception:
    pass


class UserAddForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('email', 'phone', 'first_name', 'last_name')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        self.fields['phone'].required = True
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        for f in self.fields.values():
            f.widget.attrs.setdefault('autocomplete', 'off')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_unusable_password()
        if commit:
            user.save()
        return user


class UserChangeForm(forms.ModelForm):
    class Meta:
        model = User
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        self.fields['first_name'].required = True
        self.fields['last_name'].required = True
        # Customers must have a phone number; admins do not.
        is_admin = bool(self.instance and getattr(self.instance, 'role', '') == 'admin')
        self.fields['phone'].required = not is_admin
        for f in self.fields.values():
            f.widget.attrs.setdefault('autocomplete', 'off')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['email', 'first_name', 'last_name', 'phone_display', 'role', 'is_active', 'created_at']
    # Custom User without username: the search_fields inherited from Django with 'username' caused a
    # FieldError (500) in ?q= and in the
    # "User" autocomplete on the course purchase form.
    search_fields = ('email', 'first_name', 'last_name', 'phone')
    ordering = ['-created_at']
    save_on_top = True
    change_list_template = 'admin/users/user/change_list.html'
    change_form_template = 'admin/users/user/change_form.html'
    add_form_template = 'admin/users/user/change_form.html'

    @admin.display(description='Phone', ordering='phone')
    def phone_display(self, obj):
        from orders.models import format_phone
        return format_phone(obj.phone)

    def changelist_view(self, request, extra_context=None):
        from orders.models import Order, format_phone
        from django.db.models import Max

        extra_context = extra_context or {}
        last_orders = dict(
            Order.objects.values('user_id').annotate(last=Max('created_at')).values_list('user_id', 'last')
        )
        now = timezone.now()
        users = User.objects.exclude(role='admin').order_by('-created_at')
        users_flat = []
        for u in users:
            last_order_dt = last_orders.get(u.pk)
            last_login_dt = u.last_login

            # take the more recent of the two dates
            candidates = [d for d in [last_order_dt, last_login_dt] if d]
            last_active_dt = max(candidates) if candidates else None

            if last_active_dt:
                days = (now - last_active_dt).days
                if days <= 30:
                    activity_label = 'active'
                elif days <= 90:
                    activity_label = 'idle'
                else:
                    activity_label = 'dead'
                activity_str = timezone.localtime(last_active_dt).strftime('%d.%m.%Y %H:%M')
            else:
                activity_label = 'never'
                activity_str = ''

            users_flat.append({
                'id': u.pk,
                'email': u.email,
                'first_name': u.first_name or '',
                'last_name': u.last_name or '',
                'phone': format_phone(u.phone),
                'role': u.role,
                'is_active': u.is_active,
                'created_at': timezone.localtime(u.created_at).strftime('%d.%m.%Y %H:%M') if u.created_at else '',
                'last_active': activity_str,
                'activity_label': activity_label,
            })
        # JS-safe inline: escape U+2028/U+2029 (old Safari treats them as a line
        # break in a literal -> SyntaxError) and '<' (so that </script> is not closed).
        extra_context['users_json'] = js_json(users_flat)
        return super().changelist_view(request, extra_context)

    list_filter = [IsActiveFilter]

    form = UserChangeForm
    add_form = UserAddForm
    add_fieldsets = (
        ('Main', {'fields': ('email', 'first_name', 'last_name', 'phone')}),
        ('Delivery', {'fields': ('np_city', 'np_city_ref', 'np_warehouse')}),
    )

    def get_fieldsets(self, request, obj=None):
        """For admins, a simplified set (email/full name/role/active flag).
        For customers, Main + Delivery (as before).
        """
        if obj and obj.role == 'admin':
            return (
                ('Administrator', {
                    'fields': ('email', 'first_name', 'last_name', 'phone', 'admin_role', 'is_active'),
                }),
            )
        return (
            ('Main', {'fields': ('email', 'first_name', 'last_name', 'phone')}),
            ('Delivery', {'fields': ('np_city', 'np_city_ref', 'np_warehouse')}),
        )
