"""Django admin permissions for the superuser/manager/viewer roles.

Monkey-patches `BaseModelAdmin.has_*_permission`, so that we do not patch every
ModelAdmin separately. Django itself uses these methods both to hide buttons
in the UI and to check on save, so we do not need a separate middleware.

Rules:
  superuser - everything allowed (default Django behavior).
  manager   - everything except managing other admins (User with is_staff=True)
              and SiteSettings (Settings).
  viewer    - read-only across the whole CRM.
"""
from django.contrib.admin.options import BaseModelAdmin


_ORIG = {
    'change': BaseModelAdmin.has_change_permission,
    'add':    BaseModelAdmin.has_add_permission,
    'delete': BaseModelAdmin.has_delete_permission,
}


def _user_admin_role(request):
    u = getattr(request, 'user', None)
    if u is None or not u.is_authenticated:
        return ''
    if u.is_superuser:
        return 'superuser'
    return getattr(u, 'admin_role', '') or ''


def _is_manager_restricted(modeladmin, obj):
    """Whether this model/object is closed to the manager."""
    meta = modeladmin.model._meta
    label = meta.app_label
    name = meta.model_name

    # Site settings are fully closed.
    if label == 'catalog' and name == 'sitesettings':
        return True

    # User: the manager cannot manage other admins.
    if label == 'users' and name == 'user':
        if obj is None:
            # On the add / changelist page we allow it (an admin is created only by a superuser).
            # But adding the user itself is forbidden separately in has_add_permission.
            return False
        return bool(obj.is_staff)

    return False


def _patched_change(self, request, obj=None):
    role = _user_admin_role(request)
    if role == 'viewer':
        return False
    if role == 'manager' and _is_manager_restricted(self, obj):
        return False
    return _ORIG['change'](self, request, obj)


def _patched_add(self, request):
    role = _user_admin_role(request)
    if role == 'viewer':
        return False
    if role == 'manager':
        meta = self.model._meta
        # The manager does not create new Users (admins) and does not touch SiteSettings.
        if meta.app_label == 'catalog' and meta.model_name == 'sitesettings':
            return False
        if meta.app_label == 'users' and meta.model_name == 'user':
            return False
    return _ORIG['add'](self, request)


def _patched_delete(self, request, obj=None):
    role = _user_admin_role(request)
    if role == 'viewer':
        return False
    if role == 'manager' and _is_manager_restricted(self, obj):
        return False
    return _ORIG['delete'](self, request, obj)


def patch_admin_permissions():
    BaseModelAdmin.has_change_permission = _patched_change
    BaseModelAdmin.has_add_permission = _patched_add
    BaseModelAdmin.has_delete_permission = _patched_delete
